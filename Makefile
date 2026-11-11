.PHONY: install test test-unit test-integration clean run run-dry run-issue run-issue-dry

install:            ## Install all dependencies via uv
	uv sync

test:               ## Run all tests
	uv run pytest tests/ -v --tb=short

test-unit:          ## Run unit tests only
	uv run pytest tests/test_checks.py tests/test_report.py tests/test_release_calendar.py tests/test_update_release_calendar.py -v --tb=short

test-integration:   ## Run integration tests (jira-emulator)
	uv run pytest tests/test_quality_gate.py tests/test_label_management.py -v --tb=short

run:                ## Run full batch (RICE + gate labels)
	uv run python scripts/quality_gate.py

run-dry:            ## Run full batch dry run (RICE only, no gate labels, no Jira writes)
	uv run python scripts/quality_gate.py --dry-run

run-issue:          ## Run single issue (RICE + gate labels). Usage: make run-issue ISSUE=RHAISTRAT-1745
	uv run python scripts/quality_gate.py --issue $(ISSUE)

run-issue-dry:      ## Run single issue dry run. Usage: make run-issue-dry ISSUE=RHAISTRAT-1745
	uv run python scripts/quality_gate.py --issue $(ISSUE) --dry-run

clean:              ## Remove build artifacts
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	rm -rf artifacts/*
