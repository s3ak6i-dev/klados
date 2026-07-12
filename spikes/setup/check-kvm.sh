#!/usr/bin/env bash
# Verify the host can actually run Firecracker. Fails loud and early.
set -euo pipefail

fail() { echo "FAIL: $*" >&2; exit 1; }
ok()   { echo "ok:   $*"; }

[[ "$(uname -s)" == "Linux" ]] || fail "not Linux — Firecracker needs a KVM Linux host"
ok "Linux $(uname -r)"

[[ "$(uname -m)" == "x86_64" || "$(uname -m)" == "aarch64" ]] || fail "unsupported arch $(uname -m)"
ok "arch $(uname -m)"

if [[ -e /dev/kvm ]]; then
  ok "/dev/kvm present"
  [[ -r /dev/kvm && -w /dev/kvm ]] || fail "/dev/kvm not read/writable by $(whoami) — add user to the 'kvm' group or run privileged"
  ok "/dev/kvm read/writable"
else
  fail "/dev/kvm missing — no KVM. On a cloud VM you need nested virtualization enabled; stock WSL2 will not work."
fi

# Hardware virt flag (informational; nested-virt VMs may not expose it but /dev/kvm is what matters)
if grep -Eq '(vmx|svm)' /proc/cpuinfo; then ok "cpu virt flag present (vmx/svm)"; else echo "warn: no vmx/svm flag in /proc/cpuinfo (may be a nested-virt guest)"; fi

command -v firecracker >/dev/null 2>&1 && ok "firecracker $(firecracker --version 2>/dev/null | head -1)" || echo "warn: firecracker not installed yet — run setup/provision.sh"
command -v python3 >/dev/null 2>&1 && ok "python3 $(python3 --version 2>&1)" || fail "python3 missing"

echo "check-kvm: host looks capable."
