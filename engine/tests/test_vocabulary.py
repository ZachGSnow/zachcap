from zachcap import vocabulary


def test_terms_and_what_whisper_mishears_them_as():
    words = vocabulary.parse("Proxmox: prox mocks, proxmocks\n\nContoso, ConnectWise\n  Datto  ")
    assert words.terms == ["Proxmox", "Contoso", "ConnectWise", "Datto"]
    assert words.corrections["prox mocks"] == "Proxmox"
    assert words.corrections["connectwise"] == "ConnectWise"


def test_corrections_fix_misheard_words_and_casing_but_not_inside_other_words():
    words = vocabulary.parse("Proxmox: prox mocks\nConnectWise")
    fixed = words.correct("We moved the prox mocks host; connectwise tickets too. Connectwisely.")
    assert fixed == "We moved the Proxmox host; ConnectWise tickets too. Connectwisely."


def test_hotwords_hold_terms_then_names_within_whispers_limit():
    words = vocabulary.parse("Contoso\nProxmox")
    # A sentence, not a bare list: Whisper copies the prompt's style, punctuation included.
    assert words.hotwords(["Dana", "contoso"]) == (
        "Names and terms in this meeting: Contoso, Proxmox, Dana."
    )
    many = vocabulary.parse("\n".join(f"Term{i:03d}" for i in range(200)))
    assert len(many.hotwords()) <= vocabulary.HOTWORD_CHARS
    assert vocabulary.parse("").hotwords() is None


def test_terms_with_backslashes_are_text_not_patterns():
    # Accounts, registry paths and shares. As regex templates they failed every meeting with
    # "bad escape", even when they never came up, or were changed ("\a" became a bell).
    words = vocabulary.parse(r"CORP\admin: corp admin" "\n" r"HKLM\Software" "\n" r"\\nas01")
    assert words.correct("Nothing about them here.") == "Nothing about them here."
    assert words.correct(r"Log in as corp admin, check hklm\software on \\NAS01.") == (
        r"Log in as CORP\admin, check HKLM\Software on \\nas01."
    )


def test_ordinary_words_keep_their_case():
    words = vocabulary.parse("Will\nMark\nIT\nConnectWise\nM365\nMicrosoft Teams")
    assert words.correct("I will mark it as done, will you? it's in connectwise and m365.") == (
        "I will mark it as done, will you? it's in ConnectWise and M365."
    )
    assert words.correct("join on microsoft teams") == "join on Microsoft Teams"
    assert "Will" in words.hotwords()  # still listened for


def test_a_full_word_list_on_a_long_meeting_is_quick():
    import time

    # As long a list as Settings takes (4,000 characters): 764 words and misheard forms.
    words = vocabulary.parse("\n".join(f"T{i}: a{i}, b{i}" for i in range(300))[:4000])
    line = "We went over the plan for the next release and who owns each part, a17 too."
    started = time.perf_counter()
    fixed = [words.correct(line) for _ in range(1000)]
    # One pattern per line instead of one per word: this took 18 s, with the database
    # locked for writing meanwhile.
    assert time.perf_counter() - started < 2.0
    assert fixed[0].endswith("part, T17 too.")


def test_a_fixed_line_offers_the_entry_that_would_fix_the_mishearing():
    offer = vocabulary.suggest
    assert offer(
        "we moved to cooper netties last week.", "we moved to Kubernetes last week.", ""
    ) == {
        "term": "Kubernetes",
        "misheard": "cooper netties",
    }
    assert offer("upgrade them to win 11", "upgrade them to Win11", "Win11") == {
        "term": "Win11",
        "misheard": "win 11",  # "Win11" alone only fixes the case, not the space
    }
    # A name or distinctive term written in the wrong case.
    assert offer("the proxmox host", "the Proxmox host", "") == {
        "term": "Proxmox",
        "misheard": None,
    }
    assert offer("the m365 tenant", "the M365 tenant", "") == {"term": "M365", "misheard": None}


def test_other_edits_offer_nothing():
    offer = vocabulary.suggest
    assert offer("call Dana tomorrow", "call Dana on Friday", "") is None  # what was said
    assert offer("we use teams", "we use Zoom", "") is None
    assert offer("will you mark it", "Will you mark it", "") is None  # starts a sentence
    assert offer("ticket 4512 is open", "ticket 4521 is open", "") is None
    assert offer("a b c d e", "x y z w v", "") is None  # too much changed
    assert offer("use connect wise", "use ConnectWise", "ConnectWise: connect wise") is None
    assert offer("open the c drive", "open the C:Drive", "") is None  # can't go on a line


def test_adding_an_entry_merges_into_the_terms_own_line():
    add = vocabulary.add_correction
    assert add("Proxmox\nConnectWise: connect wise", "ConnectWise", "connected wise") == (
        "Proxmox\nConnectWise: connect wise, connected wise"
    )
    assert add("proxmox", "Proxmox", "prox mocks") == "proxmox: prox mocks"
    assert add("Contoso", "M365", None) == "Contoso\nM365"
    assert add("ConnectWise: connect wise", "connectwise", "Connect Wise") == (
        "ConnectWise: connect wise"  # already there, in another case
    )
    for bad in (("Prox: mox", "x"), ("A, B", "x"), ("Term", "a, b")):
        try:
            add("", *bad)
        except ValueError:
            continue
        raise AssertionError(f"accepted {bad}")
    try:
        add("x" * 3995, "Proxmox", "prox mocks")
    except ValueError as exc:
        assert "full" in str(exc)
    else:
        raise AssertionError("accepted a list over the limit")
