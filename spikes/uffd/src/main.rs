//! Shared-base UFFD page-fault handler for Klados Spike B (`--mechanism uffd`).
//!
//! One handler serves one child VM. It mmaps the base memory-snapshot file ONCE
//! (read-only, shared) and, on each guest page fault, `UFFDIO_COPY`s the
//! corresponding page from that shared arena into the faulting VM. Because the
//! arena is a single shared mapping of the base file, the immutable base lives in
//! page cache once regardless of how many handlers run — the fork-economics claim.
//!
//! It counts faulted pages so the harness can see each child's materialized-from-base
//! working set (the divergence proxy).
//!
//! ┌─ CORRECTNESS CAVEAT (read before trusting results) ────────────────────────┐
//! │ Firecracker hands the userfaultfd object and the guest memory-region layout │
//! │ to this process over the UDS at `--socket`, using a small protocol (JSON    │
//! │ region descriptors + the uffd fd passed via SCM_RIGHTS). That protocol has  │
//! │ changed across Firecracker releases. The receive step below is modeled on   │
//! │ Firecracker's example UFFD handler and MUST be reconciled against the       │
//! │ `examples/uffd` handler shipped with the FC_VERSION you install. TODO(host).│
//! └────────────────────────────────────────────────────────────────────────────┘

use std::os::unix::io::{FromRawFd, RawFd};
use std::os::unix::net::UnixListener;
use std::sync::atomic::{AtomicU64, Ordering};

use userfaultfd::Uffd;

const PAGE: usize = 4096;

struct Args {
    socket: String,
    mem: String,
}

fn parse_args() -> Args {
    let mut socket = None;
    let mut mem = None;
    let mut it = std::env::args().skip(1);
    while let Some(a) = it.next() {
        match a.as_str() {
            "--socket" => socket = it.next(),
            "--mem" => mem = it.next(),
            other => eprintln!("ignoring unknown arg: {other}"),
        }
    }
    Args {
        socket: socket.expect("--socket <uds path> required"),
        mem: mem.expect("--mem <base mem file> required"),
    }
}

/// mmap the base memory file once, read-only + shared, as the source arena.
fn map_base(path: &str) -> (*const u8, usize) {
    let file = std::fs::File::open(path).expect("open base mem file");
    let len = file.metadata().expect("stat base mem").len() as usize;
    use std::os::unix::io::AsRawFd;
    let addr = unsafe {
        libc::mmap(
            std::ptr::null_mut(),
            len,
            libc::PROT_READ,
            libc::MAP_SHARED,
            file.as_raw_fd(),
            0,
        )
    };
    assert!(addr != libc::MAP_FAILED, "mmap base failed");
    (addr as *const u8, len)
}

/// Receive the uffd fd + guest region layout from Firecracker over the UDS.
///
/// TODO(host): reconcile this framing with your Firecracker version's example.
/// The shape is: FC connects, sends a JSON body describing guest_region mappings
/// (base_host_virt_addr, size, offset), and passes the userfaultfd as an SCM_RIGHTS
/// ancillary fd. Returns (uffd, guest_base_addr, guest_len).
fn recv_from_firecracker(listener: &UnixListener) -> (Uffd, usize, usize) {
    let (stream, _) = listener.accept().expect("accept firecracker");
    let raw = recv_fd_with_payload(&stream);
    // `raw.fd` is the userfaultfd; `raw.guest_addr`/`raw.guest_len` come from the JSON body.
    let uffd = unsafe { Uffd::from_raw_fd(raw.fd) };
    (uffd, raw.guest_addr, raw.guest_len)
}

struct Handoff {
    fd: RawFd,
    guest_addr: usize,
    guest_len: usize,
}

/// Minimal SCM_RIGHTS receive. Parses the JSON payload for the region and pulls the fd.
/// Kept deliberately small; expand to the full multi-region case if your snapshot has
/// more than one guest memory region.
fn recv_fd_with_payload(stream: &std::os::unix::net::UnixStream) -> Handoff {
    use std::os::unix::io::AsRawFd;
    let mut buf = [0u8; 4096];
    let mut cmsg = [0u8; 64];
    let mut iov = libc::iovec {
        iov_base: buf.as_mut_ptr() as *mut libc::c_void,
        iov_len: buf.len(),
    };
    let mut msg: libc::msghdr = unsafe { std::mem::zeroed() };
    msg.msg_iov = &mut iov;
    msg.msg_iovlen = 1;
    msg.msg_control = cmsg.as_mut_ptr() as *mut libc::c_void;
    msg.msg_controllen = cmsg.len();

    let n = unsafe { libc::recvmsg(stream.as_raw_fd(), &mut msg, 0) };
    assert!(n >= 0, "recvmsg failed");

    // extract the passed fd from the control message
    let mut fd: RawFd = -1;
    unsafe {
        let mut cptr = libc::CMSG_FIRSTHDR(&msg);
        while !cptr.is_null() {
            if (*cptr).cmsg_level == libc::SOL_SOCKET && (*cptr).cmsg_type == libc::SCM_RIGHTS {
                fd = *(libc::CMSG_DATA(cptr) as *const RawFd);
                break;
            }
            cptr = libc::CMSG_NXTHDR(&msg, cptr);
        }
    }
    assert!(fd >= 0, "no SCM_RIGHTS fd received from firecracker");

    // TODO(host): parse buf[..n] JSON for the region descriptor. Placeholder single
    // region covering the whole base file; replace with the real (addr,size,offset).
    let body = String::from_utf8_lossy(&buf[..n as usize]);
    let (guest_addr, guest_len) = parse_region(&body);
    Handoff { fd, guest_addr, guest_len }
}

fn parse_region(_json: &str) -> (usize, usize) {
    // TODO(host): real JSON parse of Firecracker's GuestRegionUffdMapping.
    // Returning (0,0) forces the operator to wire this up before trusting numbers.
    (0, 0)
}

fn main() {
    let args = parse_args();
    let _ = std::fs::remove_file(&args.socket);
    let listener = UnixListener::bind(&args.socket).expect("bind uds");
    let (base_ptr, base_len) = map_base(&args.mem);

    let (uffd, guest_addr, guest_len) = recv_from_firecracker(&listener);
    if guest_len == 0 {
        eprintln!("FATAL: region descriptor not wired up (parse_region TODO). Reconcile the \
                   Firecracker UFFD handoff protocol before running this mechanism.");
        std::process::exit(2);
    }

    let faults = AtomicU64::new(0);
    let mut event_buf = Default::default();
    loop {
        match uffd.read_event(&mut event_buf) {
            Ok(Some(userfaultfd::Event::Pagefault { addr, .. })) => {
                let off = (addr as usize).saturating_sub(guest_addr);
                let page_off = off & !(PAGE - 1);
                if page_off + PAGE <= base_len && page_off + PAGE <= guest_len {
                    let src = unsafe { base_ptr.add(page_off) };
                    let dst = (guest_addr + page_off) as *mut libc::c_void;
                    unsafe {
                        // copy one page from the shared base arena into the faulting VM
                        uffd.copy(src as *const libc::c_void, dst, PAGE, true)
                            .expect("uffdio_copy");
                    }
                    faults.fetch_add(1, Ordering::Relaxed);
                } else {
                    // out-of-range fault -> zero page
                    let dst = (guest_addr + page_off) as *mut libc::c_void;
                    unsafe { uffd.zeropage(dst, PAGE, true).ok(); }
                }
            }
            Ok(Some(_)) => {}
            Ok(None) => {}
            Err(e) => {
                eprintln!("uffd read_event error: {e} (faulted {} pages)", faults.load(Ordering::Relaxed));
                break;
            }
        }
    }
}
