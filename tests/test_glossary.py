"""The glossary rules: which terms a lesson gets definitions for."""

from app.glossary import Glossary

SHARED = [
    {"term": "kernel", "def": "The core of an operating system."},
    {"term": "virtual machine", "aliases": ["VM"], "def": "A pretend computer inside a real one."},
    {"term": "machine", "def": "A computer."},
    {"term": "RAM", "case": True, "def": "The computer's short-term memory."},
]


def doc(tid, concepts, glossary=(), requires=()):
    return {"tool": {"id": tid, "requires": list(requires)}, "glossary": list(glossary), "concepts": concepts}


def lesson(cid, body, level=1, requires=(), defines=()):
    return {"id": cid, "level": level, "body": body, "requires": list(requires), "defines": list(defines)}


def test_terms_taught_earlier_are_not_repeated():
    g = Glossary([doc("os", [
        lesson("os.kernel", "The **kernel** is the core.", defines=["kernel"]),
        lesson("os.vms", "Each virtual machine has its own kernel.", requires=["os.kernel"]),
    ])], SHARED)
    assert g.lesson_glossary("os.kernel") == []  # it teaches the term itself
    terms = [t["term"] for t in g.lesson_glossary("os.vms")]
    assert terms == ["virtual machine"]  # longest match wins over "machine"; kernel already taught


def test_definitions_link_to_the_lesson_that_teaches_them():
    g = Glossary([doc("os", [
        lesson("os.intro", "Programs talk to the kernel."),
        lesson("os.kernel", "The kernel is the core.", defines=["kernel"]),
    ])], SHARED)
    (k,) = g.lesson_glossary("os.intro")
    assert k["lesson"] == "os.kernel" and k["def"]


def test_plurals_case_and_code_are_handled():
    g = Glossary([doc("os", [
        lesson("os.a", "Two VMs share 4 GB of RAM, unlike the ram animal. `kernel` in code is not prose."),
    ])], SHARED)
    terms = {t["term"] for t in g.lesson_glossary("os.a")}
    assert terms == {"virtual machine", "RAM"}


def test_required_tools_teach_terms_and_lend_their_glossary():
    base = doc("linux", [lesson("linux.k", "The kernel.", defines=["kernel", "syscall"])],
               glossary=[{"term": "syscall", "def": "A request a program makes to the kernel."}])
    top = doc("docker", [lesson("docker.a", "A syscall goes to the kernel.")], requires=[{"tool": "linux", "level": 1}])
    g = Glossary([base, top], SHARED)
    assert g.lesson_glossary("docker.a") == []
    assert "syscall" in g.visible("docker")


def test_paragraph_counts_first_use_only():
    g = Glossary([doc("os", [lesson("os.a", "kernel, VM and RAM.\n\nThe kernel again.")])], SHARED)
    per = g.new_terms_by_paragraph("os.a")
    assert sorted(per[0]) == ["RAM", "kernel", "virtual machine"] and 1 not in per
