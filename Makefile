.PHONY: check test hooks

# The one command an agent (or you) must pass before pushing: unit suite,
# behaviour checks, build-tooling tests, identifier scan (with
# .identifiers.local if present). Without make: scripts/check.sh.
check:
	scripts/check.sh

test:
	python3 -m unittest discover -s tests

# Run the scan before every push from this checkout.
hooks:
	ln -sf ../../scripts/pre-push .git/hooks/pre-push
	@echo "pre-push hook installed"
