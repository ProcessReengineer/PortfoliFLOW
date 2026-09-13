Full-suite report: PortfoliFLOW (AGPL), 2026-09-11
Verdict: green. All 4,930 selected tests ran: 4,927 passed, with 0 failures and 0 errors. Both CI steps exited with code 0. Neither of CI's rerun-once flake exceptions was needed, so each result comes from a single pass.

What was tested
Repo: /home/soenke/Code/PortfoliFLOW/PortfoliFLOW, HEAD 2a2be75 (PB-D3).
Uncommitted changes included: the PB-H1 edits to web/routes/provider_credentials.py and tests/web/test_provider_credentials.py. If you commit PB-H1 as proposed, the committed tree is exactly what was tested; its report file is documentation only.
Database: dev Postgres portfoliflow-postgres, at Alembic head b034_add_trade_tickets.
Commands: the two steps from full-suite.yml with the same test selection. I only added reporting flags (-v -rfEsxX --durations --junitxml).
Results
Step	Command	Selected	Passed	Failed / Errors	Skipped	Xfailed	Time
1	pytest -m "not integration and not timing" --ignore=tests/regression	4,782	4,779	0 / 0	2	1	2:17:54
2	pytest -m "not integration and not timing" tests/regression	148	148	0 / 0	0	0	0:02:21
Total		4,930	4,927	0 / 0	2	1	2h20m (15:38 → 17:58)
The JUnit XML totals agree with the console summary. Neither log contains a single FAILED or ERROR line.

Tests that did not run as normal passes
All of these are skipped, expected to fail, or excluded on purpose.

Skipped (2):

tests/services/analytics/test_limit_coverage.py:1214: the reference workbook for this test is not in the repo.
tests/web/test_accessibility.py:74: accessibility checks are done manually (ADR-0037 §10).
Neither skip is a "Cannot reach Postgres" skip, so every database-backed test really ran.

Expected failure (1): tests/core/test_chart_theme_alternatives.py::test_all_chart_themes_have_same_schema. The light chart theme is still missing the pf.* keys (ADR-0058).

Deselected (8), same as CI:

7 routing-eval tests in tests/assistants/test_routing_eval.py, marked integration because they call a live external service.
1 timing test, tests/auth/test_local_password_backend.py::test_constant_time_unknown_user_vs_wrong_password.
Warnings
There were 161 warnings in step 1 and none in step 2. None of them affected a result. They fall into three kinds:

Starlette's deprecation of HTTP_422_UNPROCESSABLE_ENTITY.
numpy divide and precision RuntimeWarnings in edge-case tests for statistics and FX conversion.
13 pytest warnings about @pytest.mark.asyncio on non-async test_extract_subdomain* tests in the tenant-resolution tests.
State after the run
Schema: the database is still at head b034, so the regression step did not leave it downgraded.
Working tree: unchanged by the run.
Local data: the tests cleared it. The database now has 0 users and 0 investments, so run portfoliflow bootstrap and re-import before using the app locally.
Differences from CI
Python: 3.13.14 here; CI uses 3.11.
Postgres: the long-lived local dev container here, where CI starts a fresh postgres:16 service.
Not blocking, but worth passing on
full-suite.yml still lists "AI-service singleton pollution" as one of its two rerun-once flake exceptions. The test that flake came from was deleted along with the services/ai_service.py shim (ADR-0094), so that exception can no longer apply.

The logs and JUnit XML are in /tmp/claude-1000/-home-soenke-Code-PortfoliFLOW-PortfoliFLOW/0ba8c180-8032-47b2-97ac-087733879b88/scratchpad/: step1.log, step2.log, step1.xml, step2.xml and run-meta.txt.

I can save this as a report under docs/reports/; tell me the gate ID to name it after.

ok, what is still running?
Bash Check for remaining test processes and containers
IN
SP=/tmp/claude-1000/-home-soenke-Code-PortfoliFLOW-PortfoliFLOW/0ba8c180-8032-47b2-97ac-087733879b88/scratchpad; echo "== run pid $(cat $SP/run.pid)"; ps -p $(cat $SP/run.pid) -o pid,cmd || echo "suite runner: not running"; echo "== pytest / waiter / runner processes"; pgrep -af 'pytest|run-full-suite|wait-full-suite' | grep -v pgrep || echo "none"; echo "== containers"; podman ps --format '{{.Names}}  {{.Status}}'

OUT
== run pid 6209
    PID CMD
suite runner: not running
== pytest / waiter / runner processes
none
== containers
portfoliflow-postgres  Up 3 hours (healthy)
Only the dev Postgres container is still running (portfoliflow-postgres, up 3 hours, healthy). It was stopped when I began, and I started it for the test run.

Everything else has exited: the detached test run (PID 6209) finished at 17:58, and the watcher that notified me exited after it. No pytest processes are left.

Want me to stop the container with podman stop portfoliflow-postgres, putting things back as they were? If you plan to re-bootstrap and use the app locally, leave it running.

Remote Control is active · Continue here, on your phone, or at claude.ai/code
