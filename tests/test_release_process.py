"""Guards on the release pipeline's shape. Releases here are immutable once
published, so the order of these steps is not a style choice: publishing
before the HUD is attached ships a release that can never get one."""

from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent


def _assert_pytest_jobs_use_full_history(workflow_name):
    workflow = (ROOT / ".github" / "workflows" / workflow_name).read_text(encoding="utf-8")
    jobs = workflow.split("\njobs:\n", 1)[1]
    job_sections = re.finditer(
        r"(?ms)^  (?P<name>[A-Za-z0-9_-]+):\n(?:(?!^  [A-Za-z0-9_-]+:).)*",
        jobs,
    )
    for job in job_sections:
        section = job.group(0)
        if "pytest" not in section:
            continue
        checkout = re.search(
            r"(?ms)^      - uses: actions/checkout@[^\n]+\n(?P<body>.*?)(?=^      - |\Z)",
            section,
        )
        assert checkout and re.search(r"^\s+fetch-depth:\s*0\s*$", checkout.group("body"), re.MULTILINE), (
            f"{workflow_name} job {job.group('name')} runs pytest without a full-history checkout"
        )


def test_pytest_jobs_checkout_full_history():
    for workflow_name in ("ci.yml", "release.yml"):
        _assert_pytest_jobs_use_full_history(workflow_name)


def test_ci_creates_a_draft_never_a_published_release():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "gh release create" in workflow
    assert "--draft" in workflow


def test_the_publish_script_checks_before_it_publishes():
    script = (ROOT / "scripts" / "publish_release.sh").read_text(encoding="utf-8")
    upload = script.index("gh release upload")
    publish = script.index("--draft=false")
    assert script.index("isDraft") < upload < publish
    assert script.index("gh release download") < publish   # round-trip digest check first
    assert "HUD.sha256" in script and "lipo -archs" in script


def test_the_release_build_is_universal():
    build = (ROOT / "macos-widget" / "build.sh").read_text(encoding="utf-8")
    assert "--arch arm64 --arch x86_64" in build
    # One trap covers everything: a later `trap … EXIT` replaces an earlier one.
    assert "trap cleanup EXIT" in build
    assert 'cleanup() { rm -rf "$GENERATED"' in build


def test_the_release_build_expands_empty_swift_flags_safely_on_bash_32():
    # Bash 3.2 treats an empty array as unset under `set -u`, unlike modern
    # Bash. Keep the expansion conditional so shader-free builds work there.
    build = (ROOT / "macos-widget" / "build.sh").read_text(encoding="utf-8")
    safe_expansion = '${SWIFT_FLAGS[@]+"${SWIFT_FLAGS[@]}"}'
    swift_builds = [line for line in build.splitlines() if line.lstrip().startswith("swift build")]
    assert len(swift_builds) == 2
    assert all(line.endswith(safe_expansion) for line in swift_builds)


def test_the_installer_checks_with_its_own_bin_dir_on_path():
    # A fresh account has no ~/.local/bin on PATH. doctor finds the launchers
    # there, so a bare call fails and aborts the install before the service,
    # HUD and dashboard steps.
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    assert 'if ! PATH="$BIN_DIR:$PATH" "$CLI" doctor; then' in script


def test_installers_defer_migration_until_the_new_daemon_starts():
    script = (ROOT / "install.sh").read_text(encoding="utf-8")
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "export TOKENWELL_NO_MIGRATE=1" in script
    assert "$env:TOKENWELL_NO_MIGRATE = '1'" in powershell


def test_shell_installers_expand_empty_arguments_safely_under_set_u():
    install = (ROOT / "install.sh").read_text(encoding="utf-8")
    uninstall = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    assert 'main ${1+"$@"}' in install
    assert 'purge ${1+"$@"}' in uninstall
    assert '[ -z "${BASH_SOURCE[0]:-}" ] || [ "${BASH_SOURCE[0]}" = "$0" ]' in install


def test_windows_installer_restores_no_migrate_environment_value():
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    assert "GetEnvironmentVariable('TOKENWELL_NO_MIGRATE', 'Process')" in powershell
    assert "Remove-Item Env:TOKENWELL_NO_MIGRATE" in powershell
    assert "$env:TOKENWELL_NO_MIGRATE = $previousNoMigrate" in powershell


def test_windows_installer_does_not_assign_power_shell_automatic_variables():
    powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
    for name in ("PID", "HOME", "Host", "Error", "input", "args", "_", "Matches"):
        assert not __import__("re").search(rf"\${name}\s*=", powershell, __import__("re").IGNORECASE)


def test_the_uninstaller_finds_the_launcher_without_relying_on_path():
    # Without this a fresh account skips `purge` and leaves credentials behind.
    script = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    venv = script.index('"$INSTALL_ROOT/venv/bin/tokenwell"')
    local_bin = script.index('"$HOME/.local/bin/tokenwell"')
    on_path = script.index("command -v tokenwell")
    assert venv < local_bin < on_path
    assert '[ -f "$candidate" ] && [ -x "$candidate" ]' in script


def test_the_uninstaller_falls_back_only_when_the_launcher_cannot_run():
    script = (ROOT / "uninstall.sh").read_text(encoding="utf-8")
    # Not exec'd: a launcher whose interpreter is gone must reach the manual
    # removal, while purge's own exit status (a declined prompt is 1) is final.
    assert 'exec "$LAUNCHER"' not in script
    assert '"$LAUNCHER" purge ${1+"$@"} || purge_status=$?' in script
    assert "126|127)" in script
    assert '*) exit "$purge_status" ;;' in script
    assert script.index("126|127)") < script.index("removing files directly")
