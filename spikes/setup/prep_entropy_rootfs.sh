#!/usr/bin/env bash
# Build the Spike C rootfs. Injects TWO PID-1 generators that stream random samples to
# the serial console, selectable via the kernel `init=` arg:
#   /entropy            -> vanilla (no post-fork reseed)
#   /entropy_mitigated  -> reseeds userspace PRNGs from the kernel CSPRNG each iteration
#                          (proxy for the guest agent's post-fork reseed)
# Run as root. Produces /opt/klados/assets/rootfs-entropy.ext4.
#
# Sample line: SAMPLE <i> MT=<mersenne> O=<openssl> K=<kernel-urandom>
#   MT = userspace Mersenne Twister      (random.getrandbits) — frozen in snapshot
#   O  = OpenSSL DRBG                     (ssl.RAND_bytes)      — TLS client-random source
#   K  = kernel CSPRNG                    (/dev/urandom)        — self-heals if RDRAND mixed
set -euo pipefail

BASE=/opt/klados/assets/rootfs.ext4
OUT=/opt/klados/assets/rootfs-entropy.ext4
MNT=/mnt/kr-entropy

cp "$BASE" "$OUT"
mkdir -p "$MNT"
mount -o loop "$OUT" "$MNT"

cat > "$MNT/entropy_gen.py" <<'PY'
import random, ssl, sys, time
uf = open("/dev/urandom", "rb", buffering=0)
i = 0
while True:
    mt = random.getrandbits(64)
    o = ssl.RAND_bytes(8).hex()
    k = uf.read(8).hex()
    sys.stdout.write("SAMPLE %d MT=%016x O=%s K=%s\n" % (i, mt, o, k))
    sys.stdout.flush()
    i += 1
    time.sleep(0.05)
PY

cat > "$MNT/entropy_gen_mitigated.py" <<'PY'
import random, ssl, sys, time
uf = open("/dev/urandom", "rb", buffering=0)
i = 0
while True:
    # POST-FORK MITIGATION (proxy): reseed userspace PRNGs from the kernel CSPRNG, which
    # is distinct per fork. Real system does this once on the vsock FORKED event; here we
    # do it each iteration to demonstrate the mechanism clears the collision.
    random.seed(uf.read(32))
    ssl.RAND_add(uf.read(32), 32.0)
    mt = random.getrandbits(64)
    o = ssl.RAND_bytes(8).hex()
    k = uf.read(8).hex()
    sys.stdout.write("SAMPLE %d MT=%016x O=%s K=%s\n" % (i, mt, o, k))
    sys.stdout.flush()
    i += 1
    time.sleep(0.05)
PY

# --- Guest-side fork-safety files copied from spikes/guest/ (real, maintained versions):
#     sitecustomize.py (RNG hook), guest_agent.py (vsock FORKED protocol), init_protocol.
#     The application (entropy_gen.py) stays UNMODIFIED; all fix logic lives in these.
SRC="$(cd "$(dirname "$0")/../guest" && pwd)"
mkdir -p "$MNT/klados"
cp "$SRC/sitecustomize.py"    "$MNT/klados/sitecustomize.py"
cp "$SRC/guest_agent.py"      "$MNT/klados/guest_agent.py"
cp "$SRC/init_protocol"       "$MNT/init_protocol"
cp "$SRC/fidelity_workload.py" "$MNT/fidelity_workload.py"
cp "$SRC/init_fidelity"       "$MNT/init_fidelity"
chmod +x "$MNT/init_protocol" "$MNT/init_fidelity" "$MNT/fidelity_workload.py"

# clock generator (Spike E / R8): streams guest wall + monotonic clocks to console
cat > "$MNT/clock_gen.py" <<'PY'
import sys, time
while True:
    sys.stdout.write("TIME wall=%.3f mono=%.3f\n" % (time.time(), time.monotonic()))
    sys.stdout.flush()
    time.sleep(0.25)
PY
printf '#!/bin/sh\nexec /usr/bin/python3 -u /clock_gen.py\n' > "$MNT/clock"
chmod +x "$MNT/clock" "$MNT/clock_gen.py"

printf '#!/bin/sh\nexec /usr/bin/python3 -u /entropy_gen.py\n' > "$MNT/entropy"
printf '#!/bin/sh\nexec /usr/bin/python3 -u /entropy_gen_mitigated.py\n' > "$MNT/entropy_mitigated"
# transparent: UNMODIFIED app + base-image sitecustomize hook (periodic rekey, no app change)
cat > "$MNT/entropy_transparent" <<'SH'
#!/bin/sh
mount -t proc proc /proc 2>/dev/null
mount -t tmpfs tmpfs /run 2>/dev/null
mkdir -p /run/klados
exec env PYTHONPATH=/klados KLADOS_RESEED=periodic KLADOS_RESEED_MS=2 /usr/bin/python3 -u /entropy_gen.py
SH
chmod +x "$MNT/entropy" "$MNT/entropy_mitigated" "$MNT/entropy_transparent" \
         "$MNT/entropy_gen.py" "$MNT/entropy_gen_mitigated.py"
sync
umount "$MNT"
chmod a+r "$OUT"
echo "PREP_DONE -> $OUT (vanilla=/entropy, mitigated=/entropy_mitigated, transparent=/entropy_transparent)"
