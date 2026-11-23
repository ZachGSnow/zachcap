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
