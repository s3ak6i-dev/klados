# Klados spikes — convenience targets. Run on a KVM Linux host.
.PHONY: check setup spike-a spike-b spike-b-uffd uffd clean

MEM ?= 2048
FORKS ?= 16
ITERS ?= 20
CHURN ?= 512

check:
	bash setup/check-kvm.sh

setup:
	sudo bash setup/provision.sh

spike-a:
	python3 harness/spike_a.py --mem-mib $(MEM) --iterations $(ITERS) --churn-mib $(CHURN)

spike-b:
	python3 harness/spike_b.py --forks $(FORKS) --mem-mib $(MEM) --mechanism file-cow

uffd:
	cargo build --release --manifest-path uffd/Cargo.toml

spike-b-uffd: uffd
	python3 harness/spike_b.py --forks $(FORKS) --mem-mib $(MEM) --mechanism uffd

clean:
	rm -rf results/*.json /tmp/klados-spike-*
