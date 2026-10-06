from backend.app.services.render_sandbox import classify

LINUX_BROWSER = {
    "seccomp": "0",
    "seccomp_filters": 0,
    "no_new_privs": "0",
    "pid_ns": "pid:[1]",
    "net_ns": "net:[1]",
    "user_ns": "user:[1]",
}


def _linux(role, **over):
    facts = {
        "role": role,
        "pid": 10,
        "seccomp": "2",
        "seccomp_filters": 1,
        "no_new_privs": "1",
        "pid_ns": "pid:[2]",
        "net_ns": "net:[2]",
        "user_ns": "user:[2]",
    }
    facts.update(over)
    return facts


def test_linux_passes_with_own_filter_and_namespaces():
    v = classify(
        "linux",
        LINUX_BROWSER,
        [_linux("renderer"), _linux("gpu", pid_ns="pid:[1]", net_ns="net:[1]", user_ns="user:[1]")],
    )
    assert v.verdict == "pass", v.problems


def test_linux_inherited_seccomp_is_not_a_chromium_sandbox():
    browser = {**LINUX_BROWSER, "seccomp": "2", "seccomp_filters": 1}
    v = classify("linux", browser, [_linux("renderer", seccomp_filters=1), _linux("gpu", seccomp_filters=1)])
    assert v.verdict == "fail"
    assert any("inherited" in p for p in v.problems)


def test_linux_identical_namespaces_fail():
    v = classify(
        "linux",
        LINUX_BROWSER,
        [_linux("renderer", pid_ns="pid:[1]", net_ns="net:[1]", user_ns="user:[1]"), _linux("gpu")],
    )
    assert v.verdict == "fail"


def test_linux_without_no_new_privs_fails():
    v = classify("linux", LINUX_BROWSER, [_linux("renderer", no_new_privs="0"), _linux("gpu")])
    assert v.verdict == "fail"


def test_linux_unknown_filter_count_is_inconclusive():
    v = classify(
        "linux",
        {**LINUX_BROWSER, "seccomp_filters": None},
        [_linux("renderer", seccomp_filters=None), _linux("gpu", seccomp_filters=None)],
    )
    assert v.verdict == "inconclusive"


def test_missing_gpu_process_is_inconclusive():
    v = classify("linux", LINUX_BROWSER, [_linux("renderer")])
    assert v.verdict == "inconclusive"
    assert any("gpu" in p.lower() for p in v.problems)


WIN_BROWSER = {"integrity_rid": 0x4000, "restricted": False}


def test_windows_passes_with_low_restricted_renderer():
    v = classify(
        "win32",
        WIN_BROWSER,
        [
            {"role": "renderer", "pid": 1, "integrity_rid": 0x0000, "restricted": True},
            {"role": "gpu", "pid": 2, "integrity_rid": 0x1000, "restricted": True},
        ],
    )
    assert v.verdict == "pass", v.problems


def test_windows_medium_renderer_fails():
    v = classify(
        "win32",
        WIN_BROWSER,
        [
            {"role": "renderer", "pid": 1, "integrity_rid": 0x2000, "restricted": True},
            {"role": "gpu", "pid": 2, "integrity_rid": 0x1000, "restricted": True},
        ],
    )
    assert v.verdict == "fail"


def test_windows_unrestricted_renderer_fails():
    v = classify(
        "win32",
        WIN_BROWSER,
        [
            {"role": "renderer", "pid": 1, "integrity_rid": 0x1000, "restricted": False},
            {"role": "gpu", "pid": 2, "integrity_rid": 0x1000, "restricted": True},
        ],
    )
    assert v.verdict == "fail"


def test_windows_gpu_must_be_low_and_restricted_in_absolute_terms():
    renderer = {"role": "renderer", "pid": 1, "integrity_rid": 0x0000, "restricted": True}
    medium_unrestricted = {"role": "gpu", "pid": 2, "integrity_rid": 0x2000, "restricted": False}
    assert classify("win32", WIN_BROWSER, [renderer, medium_unrestricted]).verdict == "fail"
    low_unrestricted = {"role": "gpu", "pid": 2, "integrity_rid": 0x1000, "restricted": False}
    assert (
        classify("win32", {"integrity_rid": 0x2000, "restricted": False}, [renderer, low_unrestricted]).verdict
        == "fail"
    )


def test_windows_unreadable_token_is_inconclusive():
    v = classify(
        "win32",
        WIN_BROWSER,
        [
            {"role": "renderer", "pid": 1, "error": "OpenProcessToken 5"},
            {"role": "gpu", "pid": 2, "integrity_rid": 0x1000, "restricted": True},
        ],
    )
    assert v.verdict == "inconclusive"


def test_macos_unavailable_api_is_inconclusive():
    v = classify(
        "darwin",
        {"error": "sandbox_check unavailable"},
        [
            {"role": "renderer", "pid": 1, "error": "sandbox_check unavailable"},
            {"role": "gpu", "pid": 2, "error": "sandbox_check unavailable"},
        ],
    )
    assert v.verdict == "inconclusive"


def test_macos_unsandboxed_renderer_fails():
    v = classify(
        "darwin",
        {"sandboxed": False},
        [{"role": "renderer", "pid": 1, "sandboxed": False}, {"role": "gpu", "pid": 2, "sandboxed": True}],
    )
    assert v.verdict == "fail"


def test_macos_sandboxed_browser_breaks_the_control():
    v = classify(
        "darwin",
        {"sandboxed": True},
        [{"role": "renderer", "pid": 1, "sandboxed": True}, {"role": "gpu", "pid": 2, "sandboxed": True}],
    )
    assert v.verdict == "inconclusive"
