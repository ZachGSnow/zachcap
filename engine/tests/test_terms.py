"""Names and terms found in a file, for Words to know: what's offered, in what order, and what
never is."""

import base64
import random
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zachcap import attachments, everyday, terms, vocabulary
from zachcap.api import create_app
from zachcap.related import STOP_WORDS
from zachcap.store import Store

LOCAL = "http://127.0.0.1"  # the engine only answers requests to this PC (gate.py)

AUTH = {"Authorization": "Bearer test-token"}


def found(text: str, known=(), limit: int = terms.LIMIT) -> list[str]:
    return [find["term"] for find in terms.found_in(text, known, limit)]


def counts(text: str) -> dict[str, int]:
    return {find["term"]: find["count"] for find in terms.found_in(text, [])}


# People -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        "From: Dana Lee <dana@example.com>",
        'From: "Lee, Dana" <dana@example.com>',
        'From: "Lee, Dana (Contoso IT)" <dana@example.com>',
        "From: Lee, Dana",
        "From: Dana Lee [External]",
        "From: Dr. Dana R. Lee Jr. <dana@example.com>",
        "From: DANA LEE <dana@example.com>",
        "From: Dana Lee via DocuSign <dse@example.com>",
        "From: Dana Lee - Contoso <dana@example.com>",
        "> > From: Dana Lee",
        "*From:* Dana Lee <dana@example.com>",
        "Von: Lee, Dana",
        "De : Dana Lee <dana@example.com>",
    ],
)
def test_a_persons_name_comes_from_an_email_header(header: str):
    assert [term for term in found(header) if term not in ("Contoso", "DocuSign")] == ["Dana Lee"]


def test_everyone_in_the_headers_is_found_first_and_counted_once_each():
    text = (
        'From: "Nair, Priya" <priya@example.com>\n'
        "To: Dana Lee <dana@example.com>, Marcus Ruiz <marcus@example.com>\n"
        "Cc: Vogt, Anneliese; Kowalczyk, Tomasz; Service Desk <help@example.com>; x@example.com\n"
        "Subject: RE: the Proxmox hosts\n\n"
        "We moved Proxmox again. The Proxmox hosts are patched, so Proxmox is done.\n"
    )
    people = ["Priya Nair", "Dana Lee", "Marcus Ruiz", "Anneliese Vogt", "Tomasz Kowalczyk"]
    assert found(text) == [*people, "Proxmox"]
    assert counts(text) == {**dict.fromkeys(people, 1), "Proxmox": 4}


def test_roles_and_mailboxes_in_the_headers_are_no_names():
    assert found("From: Contoso Support <x@example.com>") == ["Contoso"]
    for sender in (
        "Help Desk", "No Reply", "Accounts Payable", "Front Desk", "IT", "You", "Will",
        "dana@example.com", "dlee", "Guest Wi-Fi", "QWERTZ", "360",
    ):  # fmt: skip
        assert found(f"From: {sender} <x@example.com>") == [], sender
    # An everyday word can be a name, as far as a first name can tell.
    assert found("From: Mark Banks <mark@example.com>") == ["Mark Banks"]


def test_names_with_particles_accents_and_other_spellings_stay_whole():
    text = (
        "Attendees: Maria de la Cruz, Siobhan O'Brien, Jean-Luc Moreau and DeShawn Vega\n"
        "Owner: J\xf6rg M\xfcller-L\xfcdenscheid\n"
    )
    assert found(text) == [
        "Maria de la Cruz", "Siobhan O'Brien", "Jean-Luc Moreau", "DeShawn Vega",
        "J\xf6rg M\xfcller-L\xfcdenscheid",
    ]  # fmt: skip
    assert found("To: van Dyke, Anna; O'Brien, Siobhan") == ["Anna van Dyke", "Siobhan O'Brien"]
    in_text = "We asked Dana R. Lee, Bo Larkin and Maria de la Cruz about it, or J. Zorvex."
    assert found(in_text) == ["Dana Lee", "Bo Larkin", "Maria de la Cruz", "Zorvex"]


def test_a_first_name_is_part_of_the_whole_one():
    text = (
        "Dana Lee <dana@example.com> will drive. We asked Dana twice and Dana said yes.\n"
        "From: Dana Lee <dana@example.com>\nAfter that, call Priya about it.\n"
    )
    assert counts(text) == {"Dana Lee": 2, "Priya": 1}


def test_a_title_says_its_a_name():
    text = "Please ask Mr. Ruiz or Dr. Emeka Nwosu about it today."
    assert set(found(text)) == {"Emeka Nwosu", "Ruiz"}


# Names and terms in sentences -------------------------------------------------------------


def test_a_word_capitalized_in_the_middle_of_a_sentence_is_a_name():
    text = "We moved the mail to Mimecast last week. Yesterday the people at Fabrikam asked why."
    assert set(found(text)) == {"Mimecast", "Fabrikam"}


def test_the_capital_that_starts_a_sentence_says_nothing():
    text = "Regardless of that, it works. Nobody noticed! Everyone went home: kindly reply by noon."
    assert found(text) == []


def test_a_word_the_file_also_writes_in_lower_case_is_no_name():
    assert found("The people at Bramble said so. They pick a bramble every fall.") == []
    assert found("The people at Bramble said so. They sell jam every fall.") == ["Bramble"]


def test_names_of_two_or_three_words_stay_whole_with_an_everyday_word_in_them():
    text = (
        "The auditors from Woodgrove Bank met with Alpine Ski House and with Baker & Larkin. "
        "After that we called Blue Yonder Airlines about the invoice."
    )
    assert set(found(text)) == {
        "Woodgrove Bank", "Alpine Ski House", "Baker & Larkin", "Blue Yonder Airlines",
    }  # fmt: skip
    # More capitals in a row than a name has: a headline, so only its unusual words.
    assert set(found("Why Zorvex Bank Picked Qualtrane For Its Offices")) == {"Zorvex", "Qualtrane"}


def test_what_comes_before_or_after_a_name_isnt_part_of_it():
    text = (
        "When Northwind moves, the Fabrikam Project starts. Please email the Datto Portal team. "
        "The Contoso's contract ends in May, and Tailspin's in June. Call Litware Monday. We run "
        "the Zorvex 'Qualtrane Brindle' job, an HPE Lumley, and the Yonker VPN as well."
    )
    assert set(found(text)) == {
        "Northwind", "Fabrikam", "Datto", "Contoso", "Tailspin", "Litware", "Zorvex",
        "Qualtrane Brindle", "Lumley", "Yonker",
    }  # fmt: skip


def test_headings_and_forms_of_everyday_words_are_no_names():
    text = (
        "Project Overview\n\nNext Steps\n\nExecutive Summary and Scope of Work\n\n"
        "Key Dates: November 12\nStatus: In Progress\nPriority: High\nPrepared For: Review\n"
        "- Action Items From Last Quarter\n- Meeting Agenda\n1. Welcome and Introductions\n"
        "CONFIDENTIALITY NOTICE: This Message Is Private\nTHANK YOU FOR YOUR BUSINESS\n"
        "Planned, Planning And Plans\nApplied Copies Of Boxes\nQuickly Follow-up On E-mail\n"
        "Wi-Fi, VMs And Offsite Ability\nReassuringly Lumbering\n"
    )
    assert found(text) == []


def test_a_name_at_the_start_of_a_line_or_in_a_list_counts_last():
    text = "- Zorvex is what we use now\n- Retire the Brindle and see why\nQualtrane Larkin\n"
    assert found(text) == ["Brindle", "Qualtrane Larkin", "Zorvex"]


def test_a_label_that_comes_again_is_a_forms():
    form = "Steward: Dana Lee\nZorble: 12\n\nSteward: Priya Nair\nZorble: 4\n"
    assert set(found(form)) == {"Dana Lee", "Priya Nair"}
    assert "Zorble" in found("Zorble: the new hypervisor we run in the lab")
    # A label's value is as it's written there.
    assert found("Vendor: Blue Yonder\nClient: Alpine Ski House\nSite: Tacoma") == [
        "Blue Yonder", "Alpine Ski House", "Tacoma",
    ]  # fmt: skip


def test_a_glossarys_terms_are_found_as_written():
    text = (
        "Vintrace: the winery production system, hosted in the cloud.\n"
        "Tall Willow: the vineyard that sells us grapes.\n"
        "Marcus Labs: they supply yeast and barrels to us.\n"
        "Must: what the grapes are before they ferment.\n"
    )
    assert set(found(text)) == {"Vintrace", "Tall Willow", "Marcus Labs"}


def test_a_companys_ending_says_what_comes_before_is_its_name():
    text = "This is between Pacific Cascade Supply Co. and Adventure Works Cycles, Inc. of Tacoma."
    assert set(found(text)) == {"Pacific Cascade Supply", "Adventure Works Cycles", "Tacoma"}
    assert found("Contoso Managed Services, LLC\n") == ["Contoso Managed Services"]


def test_a_plain_tail_stays_only_if_the_name_never_stands_alone():
    assert found("Falkner Ridge (our dealer)\n") == ["Falkner Ridge"]
    assert counts("Fishbowl Licensing\n\nWe asked the people at Fishbowl about it.\n") == {
        "Fishbowl": 2
    }
    text = "We run Fishbowl Advanced and Fishbowl Drive, so Fishbowl is it."
    assert counts(text) == {"Fishbowl": 3}
    # An unusual word in it is a name of its own.
    assert counts("We run Datto Zorvex. The Datto is ours, and so is the other Datto.") == {
        "Datto Zorvex": 1
    }


def test_a_compounds_name_is_found():
    text = (
        "We use a Proxmox-based cluster, the Datto-hosted copy, a non-Veeam one, Litware-Nightly."
    )
    assert set(found(text)) == {"Proxmox", "Datto", "Veeam", "Litware"}


def test_the_kinds_come_in_order_of_use():
    text = (
        "To: Dana Lee <dana@example.com>\n"
        "We asked the people at Litware and those from Woodgrove Bank about the OneDrive copy,\n"
        "and the MX84 with our own JTAC as well.\n"
        "Zorvex Qualtrane\nBrindlewood\n"
    )
    assert found(text) == [
        "Dana Lee", "OneDrive", "Woodgrove Bank", "Litware", "MX84", "Zorvex Qualtrane",
        "Brindlewood", "JTAC",
    ]  # fmt: skip
    # More often counts too, up to a point.
    often = "The people at Litware and at Datto said so. We asked Datto, and Datto agreed."
    assert found(often) == ["Datto", "Litware"]
    assert found("Brindlewood\n" * 50 + "To: Dana Lee\n") == ["Dana Lee", "Brindlewood"]


def test_at_most_forty_are_offered():
    names = [f"Zor{a}{b}ex" for a in "bcdfglm" for b in "bcdfglm"]
    many = " ".join(f"We asked the people at {name} about it." for name in names)
    assert len(names) == 49 and found(many) == names[:40]
    assert found(many, limit=3) == names[:3]


# Capitals inside, letters and digits ------------------------------------------------------


def test_capitals_inside_make_a_term():
    text = (
        "Order ConnectWise, vCenter, OneDrive, VMware, ESXi, pfSense, AutoCAD and LogMeIn for us."
    )
    assert set(found(text)) == {
        "ConnectWise", "vCenter", "OneDrive", "VMware", "ESXi", "pfSense", "AutoCAD", "LogMeIn",
    }  # fmt: skip
    # Written small elsewhere, it's still a term; spelled two ways, it's as it is most often.
    assert counts("ConnectWise, then ConnectWise and connectwise") == {"ConnectWise": 2}
    assert counts("ConnectWise, then ConnectWise and then Connectwise") == {"ConnectWise": 3}


def test_identifiers_plurals_and_random_letters_are_no_terms():
    text = (
        "lastLogonDate userName UserName GetMagic NullReferenceException LastSeen MaxRetryCount\n"
        "VMs PCs APIs SLAs iPhone iPads LinkedIn\nQmFy aGVsbGZv xKfRtWq ThisIsAVeryLongOne\n"
        "<ZorbleBot> [ZorbleBot] $ZorbleBot {ZorbleBot} ZorbleBot(x) ZorbleBot=3 `ZorbleBot`\n"
        'Zorble_Bot Zorble.Bot x -ZorbleBot "ZorbleBot": 3, #ZorbleBot /ZorbleBot/x ~ZorbleBot\n'
    )
    assert found(text) == []
    for text in ("[ZorbleBot](https://example.com)", '"ZorbleBot"', "(ZorbleBot)", "*ZorbleBot*"):
        assert found(text) == ["ZorbleBot"], text
    assert found("- ZorbleBot\n-QuuxBot\n_BazBot_ and **FooBaz**") == [
        "ZorbleBot", "QuuxBot", "BazBot", "FooBaz",
    ]  # fmt: skip


def test_product_names_with_digits_are_found_and_ids_arent():
    prose = "We moved the mail to M365 in May and the old MX84 is next, with 3CX after that."
    assert set(found(prose)) == {"M365", "MX84", "3CX"}
    assert "MX84" in found("The MX84 is out of support anyway, so we have to see.")
    assert {"ZD421", "Zebra"} <= set(found("A Zebra ZD421 and a ThinkPad are on the desk"))
    assert "R740" in found("the Dell R740")
    ids = (
        "Please check PC14, WS12 and SRV02 in room B204 by the end of Q3, in FY26 or H2 at most. "
        "The port XG12 and ticket CW88 are for P1 and version V2, at 9AM or 10PM with 16GB. "
        "Then look at CT114, CT115 and XQ9981, the 2ND and the A4 paper."
    )
    assert found(ids) == []
    assert found("XG12\nCW88\n") == []
    column = "Tag,Model,Site\nA,MX84,x\nB,MS225,y\nC,DL380,z\n"
    assert set(found(column)) == {"MX84", "MS225", "DL380"}


def test_initials_come_last_and_common_ones_not_at_all():
    text = "We run our own JTAC with the VPN, the DNS and a PDF. The people at Proxmox said so."
    assert found(text) == ["Proxmox", "JTAC"]
    assert found("PLEASE DO NOT OPEN THE QWERTZ DOOR") == []
    assert found("It was in the old QX and the long QWERTZUI of ours.") == []


# Never --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "The admin password is BlueHeron for now.",
        "The new passcode of the safe is Blue Heron",
        "Wi-Fi key: BlueJay",
        "Our API token is BlueJay",
        "MFA goes to BlueHeron",
        "The PIN of Bramble changed",
        "Password of the NAS:\nBlueHeron is the new one, I'll text you the rest.",
        "The password changed last week.\n\nBlueHeron it is, from the people at Bramble.",
        "Passwords\n\nRouter BlueHeron\nSwitch GreenMaple\nThe lab has RedFalcon\n",
        "Password List\nBlueHeron\nGreenMaple",
        "Credentials:\n- router: BlueHeron\n- switch: GreenMaple\n- camera: RedFalcon\n",
        "## Passwords\n\nThe list:\n\n- BlueHeron\n\n- GreenMaple\n",
        "[Credentials]\nrouter BlueHeron\nswitch GreenMaple\n",
        "Here's the password of it.\n\nGuest network:\nBlueHeron\nGreenMaple\n",
        "Name,Password\nrouter,BlueHeron\nswitch,GreenMaple\n",
        "Guest Wi-Fi: BlueHeron",
        "The wifi is BlueHeron",
        "SSID = BlueHeron",
        "Guest Wi-Fi - BlueHeron",
        "NAS admin / BlueHeron",
        "admin: BlueHeron",
        "root \u2013 BlueHeron",
        "WPA2: BlueHeron",
        "TOTP seed: BlueHeron",
        "Safe combination: BlueHeron",
        "Security answer: BlueHeron",
        "His code word is BlueHeron",
        "DB_PASSWORD=x from BlueHeron",
        'adminPassword "BlueJay"',
        '"clientSecret": "BlueJay",',
        "smtpPass: BlueJay",
        "pw for the portal\nBlueHeron was the old one",
        "Das Kennwort f\xfcr das Gast-WLAN h\xe4ngt aus: BlueHeron",
        "Das Passwort lautet BlueHeron",
        "La contrase\xf1a es BlueHeron",
        "La clave del router es BlueHeron",
        "Le mot de passe est BlueHeron",
        "mdp BlueHeron",
        "La cl\xe9 est BlueHeron",
        "A senha mudou, a nova \xe9 BlueHeron",
        "La chiave \xe8 BlueHeron",
        "Het wachtwoord is\nBlueHeron en wordt gewijzigd",
        "Keys\n-----\nBlueHeron\nGreenMaple",
    ],
)
def test_nothing_on_or_after_a_line_about_passwords_is_offered(text: str):
    assert found(text) == []


@pytest.mark.parametrize(
    "text",
    [
        # Among other lines: a heading, and the names of two columns.
        "It's all in here.\n**Passwords**\n- router: BlueHeron\n- switch: GreenMaple\n",
        "It's all in here.\nwifi passwords\nguest BlueHeron\noffice GreenMaple\n",
        "It's all in here.\nLogins (incl. new site)\nguest BlueHeron\noffice GreenMaple\n",
        "It's all in here.\nPasswords Of All The Sites\nguest BlueHeron\noffice GreenMaple\n",
        "It's all in here, these are the passwords:\nguest BlueHeron\noffice GreenMaple\n",
        "It's all in here.\nDevice   Password\nrouter   BlueHeron\nswitch   GreenMaple\n",
        "It's all in here.\nDevice\tPassword\nrouter\tBlueHeron\nswitch\tGreenMaple\n",
        "It's all in here.\nName,Password\nrouter,BlueHeron\nswitch,GreenMaple\n",
        "It's all in here.\nsite name and room;password\na;BlueHeron\nb;GreenMaple\n",
        # A heading of more cells than its rows have.
        "It's all in here.\nName,Serial no.,Where it is,User name,Password,Notes,\n"
        "a,b,c,d,BlueHeron,e\nf,g,h,i,GreenMaple,j\nk,l,m,n,RedFalcon,o\n",
        # A list with commas isn't a table.
        "The router, the password, is:\nBlueHeron, a, b\nGreenMaple, c, d\nRedFalcon, e, f\n",
        # Another line about them in their list.
        "Passwords:\n- admin password BlueHeron\n- GreenMaple\n- RedFalcon\n",
        "## Passwords\n\n- BlueHeron\n- the switch's password\n- GreenMaple\n- RedFalcon\n",
        # Under it.
        "- Wi-Fi and keys\n  - BlueHeron\n  - GreenMaple\n",
        "The password is further down.\n  x\n  BlueHeron\n  GreenMaple\n",
        "## Passwords\n\n### Router\n\nBlueHeron\n\n### Switch\n\nGreenMaple\n",
        "## Keys\n\n### A password\n\nBlueHeron\n\n### Switch\n\nGreenMaple\n",
        "5. Passwords\n5.1 Router\nBlueHeron\n5.2 Switch\nGreenMaple\n",
        "# passwords for all of it\n# keep them safe\nrouter BlueHeron\nswitch GreenMaple\n",
        '{"credentials": {\n    "router": "BlueHeron",\n    "switch": "GreenMaple"\n}}\n',
        "<passwords>\n  <router>BlueHeron</router>\n  <switch>GreenMaple</switch>\n</passwords>\n",
        # A paragraph each, as a page's cells and a mail's lines are.
        "Here are the passwords:\n\nRouter: BlueHeron\n\nFront desk switch: GreenMaple\n\n"
        "The one in the room next to it: RedFalcon\n\na switch\tSilverBirch\n",
        "Passwords\n\nThe list below is an old one.\n\nBlueHeron\n\nGreenMaple\n\nRedFalcon\n",
        "Device\n\nPassword\n\nrouter\n\nBlueHeron\n\nfront desk switch\n\nGreenMaple\n",
        "Device  Password\n\nrouter  BlueHeron\n\na switch at the very front desk  GreenMaple\n",
        "Passwords:\n\n  router: BlueHeron\n\n  switch: GreenMaple\n",
    ],
)
def test_nothing_a_line_about_passwords_heads_is_offered(text: str):
    assert found(text) == []


def test_nothing_in_a_saved_pages_table_of_passwords_is_offered(tmp_path: Path):
    cells = [("<th>Device</th>", "<th>Password</th>"), ("<td>Router</td>", "<td>BlueHeron</td>")]
    cells.append(("<td>Front desk</td>", "<td>GreenMaple</td>"))
    rows = [f"<tr>\n    {name}\n    {value}\n  </tr>" for name, value in cells]
    table = "<h1>Devices</h1>\n<table>\n  " + "\n  ".join(rows) + "\n</table>\n"
    text = "<p>We asked the people at Fabrikam.</p>"
    page = tmp_path / "page.html"
    for saved in (table + text, table.replace("\n", "") + "<br>\n<br>\n" + text):
        page.write_text(saved)
        assert found(attachments.read(page)[1]) == ["Fabrikam"]


@pytest.mark.parametrize(
    "text",
    [
        "The password changed last week.\nNothing is known of it here.\n{}",
        "It's in the office.\nPassword List\nNothing here.\n\n{}",
        "The Passwords Of All Our Sites\nrouter x\n{}",  # too long for a heading
        "password list\nrouter x\n{}",
        "Password Change: May\nrouter x\n{}",
        "  set type password\n  set x y\n  {}",
        "the password changed, so the others, the two in the lab and the spare\na switch\n{}",
        "[10:31] the password\n[10:32] x\n{}",
        "Passwords:\nBlueHeron\nGreenMaple\n\n{}",
        "Passwords:\n\nBlueHeron\n\nGreenMaple\n\n{}",
        "Passwords:\n\nBlueHeron\n\nThanks!\n\n{}",
        "## Passwords\n\nBlueHeron\n\n## Vendors\n\n{}",
        "## Passwords\n\n## Vendors\n\n{}",
        "### Passwords\n\nBlueHeron\n\n# Vendors\n\n{}",
        "Passwords:\nBlueHeron\n# Vendors\n{}",
        '{{\n  "password": 3,\n  "notes": [\n    "{}"\n  ]\n}}',
        "1. The password of the NAS:\n   BlueHeron\n   GreenMaple\n2. {}",
        "- Passwords:\n  - BlueHeron\n  - GreenMaple\n- {}",
        "{}\n\nPassword List",
    ],
)
def test_the_text_goes_on_after_that(text: str):
    assert found(text.format("We asked the people at Fabrikam about it.")) == ["Fabrikam"]


def test_a_list_of_passwords_is_over_at_a_sentence():
    listed = "Passwords:\n\nBlueHeron\n\n"
    assert found(listed + "It's from Fabrikam.\n") == ["Fabrikam"]
    assert found(listed + "Was it Fabrikam?\n\nOr Contoso\n") == ["Fabrikam", "Contoso"]
    assert found(listed + "the people at Fabrikam know all of it\n") == ["Fabrikam"]
    assert found(listed + "or Fabrikam\n\nor Contoso\n") == []


def test_a_row_about_a_password_hides_itself_and_the_next():
    table = (
        "Ticket,Person,About\n1,Dana Lee,x\n2,Priya Nair,A password\n3,Aiko Tanaka,y\n"
        "4,Marcus Ruiz,Sage Intacct\n"
    )
    assert set(found(table)) == {"Dana Lee", "Marcus Ruiz", "Sage Intacct"}


def test_two_lines_make_a_table_but_with_commas_three_do():
    assert found("First name\tLast name\tRoom\nJorvik\tHalloran\tx\n") == ["Jorvik Halloran"]
    two = "Team, status, site\nBramble, Fabrikam, x\n"
    assert "Fabrikam" in found(two)
    assert "Fabrikam" not in found(two + "A, b, c\n")


def test_passwords_and_codes_themselves_are_never_offered():
    text = "Use hunter2, P@ssw0rd, BlueFish42, Blue-Fish-Tree, Tr0ub4dor or Blue_Heron for the lab."
    assert found(text) == []


def test_a_tables_column_of_passwords_is_never_looked_in():
    table = (
        "Name,Site,Admin PIN,Notes\n"
        "Dana Lee,Tacoma,BlueHeron,x\n"
        "Priya Nair,Spokane,GreenMaple,Sage Intacct\n"
        "Marcus Ruiz,Yakima,RedFalcon,y\n"
    )
    # The row right after the heading goes with it; after that, only the column.
    assert set(found(table)) == {
        "Priya Nair", "Marcus Ruiz", "Spokane", "Yakima", "Sage Intacct",
    }  # fmt: skip
    piped = (
        "| Name | Password | Site |\n|---|---|---|\n| A | B | C |\n"
        "| Dana Lee | BlueHeron | Tacoma |\n"
    )
    assert set(found(piped)) == {"Dana Lee", "Tacoma"}


def test_a_row_that_doesnt_fit_a_table_with_passwords_isnt_looked_in():
    table = (
        "Name,Site,Password\nA,B,C\nDana Lee,Tacoma,BlueHeron\nPriya Nair,Spokane,GreenMaple\n"
        "Marcus Ruiz, Jr,Yakima,RedFalcon\nBramble,SilverBirch\n"
        "a,b,c,d,MarshHarrier\na,b,c,d,MarshHarrier\na,b,c,d,MarshHarrier\n\n"
        "The people at Fabrikam know more about it.\n"
    )
    assert set(found(table)) == {"Dana Lee", "Priya Nair", "Tacoma", "Spokane", "Fabrikam"}


def test_key_people_and_dates_are_no_keys():
    text = "Key contacts:\n- Dana Lee\n- Priya Nair\nKey dates are with the people at Fabrikam too."
    assert set(found(text)) == {"Dana Lee", "Priya Nair", "Fabrikam"}
    assert found("The key is with Dana Lee") == []


def test_ids_addresses_paths_numbers_and_dates_are_never_offered():
    text = (
        "Serial 8FK2Q13 and PF3XK2M1, Q2MN-AB12-CD34, asset CT-0101, host SEA-LT-0142, SQL01.\n"
        "Ids 3f2504e0-4f89-11d3-9a0c-0305e82c3301, 0x80070005, DEADBEEF01, 00:1A:2B:3C:4D:5E\n"
        "Write to Dana.Lee@Contoso.example or see https://Contoso.example/Docs, www.Bramble.example\n"
        "C:\\Program Files\\Bramble\\Tool.exe, \\\\NAS01\\Public, /usr/share/Zorble, CORP\\Bram\n"
        "Call (253) 555-0142 ext. 12 on Monday, November 23rd at 9:00 AM Pacific, or Tue 24 Nov.\n"
        "It costs $1,200.00 USD, EUR 300 or 5% of it in the Spring; version v12.1.2, Zorble.Bot\n"
        "svc_Bramble, #Bramble, @Bramble, %Bramble%, Bramble$, QmFzZTY0IGVuY29kZWQ+dGV4dA==\n"
    )
    assert found(text) == []


def test_greetings_and_signatures_are_never_offered():
    text = (
        "Good Morning Team,\n\nThanks Again!\n\nBest Regards,\nKind Regards,\nMany Thanks\n"
        "Sent from my iPhone\nGet Outlook for iOS\nMit freundlichen Gr\xfc\xdfen\nCordialement,\n"
        "Senior Systems Engineer\nOffice: 253-555-0142 | Mobile: 253-555-0177\n"
        "CAUTION: This email originated from outside of the organization.\n"
    )
    assert found(text) == []


def test_everyday_names_whisper_spells_right_are_left_out():
    text = "We opened Word, Excel and Outlook on Windows in Seattle for the Microsoft people."
    assert found(text) == []


def test_terms_already_listed_in_any_capitals_are_left_out():
    text = "We use ConnectWise and Proxmox with Dana Lee and Priya Nair at Fabrikam every day."
    assert set(found(text)) == {"ConnectWise", "Proxmox", "Dana Lee", "Priya Nair", "Fabrikam"}
    known = ["connectwise", "PROXMOX", "dana lee"]
    assert set(found(text, known)) == {"Priya Nair", "Fabrikam"}
    # A listed name's parts are listened for with it.
    assert found("We asked Dana about it and then Nair.", ["Dana Lee", "Priya Nair"]) == []


# Tables -------------------------------------------------------------------------------

ASSETS = """Tag,Host,Type,Make,Model,Serial,Assigned To,Department,Site,Notes
CT-0101,TAC-LT-0101,Laptop,Dell,Latitude 5440,8FK2Q13,Dana Lee,Finance,Tacoma,Sage Intacct
CT-0102,TAC-LT-0102,Laptop,Lenovo,ThinkPad T14,PF3XK2M1,Priya Nair,Sales,Tacoma,Dock at home
CT-0103,TAC-LT-0103,Laptop,Lenovo,ThinkPad T14,PF3XK2M2,Siobhan O'Brien,Sales,Tacoma,Spare
CT-0104,TAC-DT-0104,Desktop,Dell,OptiPlex 7090,4JQ8TT3,Front Desk,Reception,Tacoma,In use
CT-0105,TAC-SV-0105,Server,HPE,ProLiant DL380,CZ2301AB7K,,IT,Tacoma,Veeam repository
CT-0106,TAC-FW-0106,Firewall,Fortinet,FortiGate 60F,FGT60FTK2101,,IT,Tacoma,Replace soon
CT-0107,SPO-FW-0107,Firewall,Cisco Meraki,MX84,Q2MN-AB12-CD34,,IT,Spokane,Runs Fishbowl
CT-0108,SPO-PH-0108,Phone,Yealink,T54W,805E0C123456,Mark Banks,Sales,Spokane,3CX extension 412
"""


def test_an_asset_list():
    offered = found(ASSETS)
    people = ["Dana Lee", "Priya Nair", "Siobhan O'Brien", "Mark Banks"]
    assert set(people) <= set(offered[:6])
    assert set(offered) == {
        *people, "Sage Intacct", "Latitude", "Lenovo", "ThinkPad", "OptiPlex", "ProLiant", "Veeam",
        "Fortinet", "FortiGate", "Cisco Meraki", "Fishbowl", "Yealink", "Tacoma", "Spokane", "T14",
        "DL380", "MX84", "T54W", "3CX",
    }  # fmt: skip
    assert counts(ASSETS)["Tacoma"] == 6 and counts(ASSETS)["ThinkPad"] == 2


def test_names_in_two_columns_in_capitals_or_last_name_first():
    exported = (
        "First Name,Middle Name,Last Name,Company,Job Title,E-mail Address\n"
        "Beatrix,,Vandermeer,Graphic Design Institute,Dean of Students,beatrix@example.com\n"
        "Jorvik,A,Halloran,Falkner Ridge Systems,Render Wrangler,jorvik@example.com\n"
        "Imogen,,Thackeray,Zorvex Technologies,Account Executive,imogen@example.com\n"
    )
    assert found(exported)[:3] == ["Beatrix Vandermeer", "Jorvik Halloran", "Imogen Thackeray"]
    assert set(found(exported)[3:]) == {"Falkner Ridge Systems", "Zorvex Technologies"}
    capitals = (
        "ID;EMPLOYEE NAME;SUPERVISOR;STATUS\n"
        '1;"OSEI, PHILIPPA A";"MARSH, QUENTIN";ACTIVE\n'
        '2;"VAN DER MEER, JOOST";"MARSH, QUENTIN";ACTIVE\n'
        '3;"O\'SHAUGHNESSY, CILLIAN";"OSEI, PHILIPPA";LEAVE\n'
    )
    assert counts(capitals) == {
        "Philippa Osei": 2, "Quentin Marsh": 2, "Joost van der Meer": 1,
        "Cillian O'Shaughnessy": 1,
    }  # fmt: skip


def test_a_tables_headings_are_no_names_in_any_language():
    table = "Nr;Hersteller;Modell;Bemerkung\n1;Lenovo;ThinkPad;x\n2;Fujitsu;Lifebook;y\n"
    assert set(found(table)) == {"Lenovo", "ThinkPad", "Fujitsu", "Lifebook"}


def test_sentences_with_commas_are_no_table():
    text = (
        "We met with the people at Fabrikam, then Contoso, about it\n"
        "and we agreed with the people at Litware, and with Datto, on it\n"
    )
    assert set(found(text)) == {"Fabrikam", "Contoso", "Litware", "Datto"}


def test_columns_set_out_with_spaces_are_read_apart():
    text = (
        "Ngozi Adeyemi       Chief Officer     2101\nMaria de la Cruz    Teller            2140\n"
    )
    assert found(text) == ["Ngozi Adeyemi", "Maria de la Cruz"]


# Other languages and typing ---------------------------------------------------------------

GERMAN = """Von: Schreiner, Lieselotte <l.schreiner@example.com>
An: Lee, Dana; Tanaka, Aiko
Betreff: Angebot f\xfcr den Umzug

Guten Morgen Frau Lee,

vielen Dank f\xfcr das Angebot. Wir haben es am Freitag mit Herrn Hofreiter und der Leitung
besprochen und sind einverstanden. Die Verkabelung ist fertig, und die Abnahme erfolgt am
Freitag. Die Sicherung l\xe4uft zurzeit noch \xfcber Veeam auf das Bandlaufwerk. Die FortiGate
ist im Keller, mit Verz\xf6gerung. Unser Standort Bamberg ist \xfcber eine Richtfunkstrecke
angebunden. Die Proxmox-Server und der Termin mit der Firma Br\xfcckner aus Kitzingen sind
f\xfcr den Dezember geplant. Die Assistentin der Gesch\xe4ftsleitung kommt auch.

Mit freundlichen Gr\xfc\xdfen
Nordwind Handels GmbH
"""


def test_german_nouns_are_no_names():
    offered = found(GERMAN)
    assert offered[:3] == ["Lieselotte Schreiner", "Dana Lee", "Aiko Tanaka"]
    assert set(offered) == {
        "Lieselotte Schreiner", "Dana Lee", "Aiko Tanaka", "Hofreiter", "Veeam", "FortiGate",
        "Bamberg", "Proxmox", "Br\xfcckner", "Kitzingen", "Nordwind Handels",
    }  # fmt: skip


def test_line_starts_in_another_language_are_no_names():
    french = (
        "Compte rendu de la r\xe9union\n\nT\xe9l\xe9phonie\n"
        "Le passage \xe0 Wildix est pr\xe9vu pour le mois de mai.\nSauvegardes\n"
        "Aiko pr\xe9sente le nouveau Datto \xe0 toute l'\xe9quipe de la ville.\nDivers\n"
        "Prochaine visite \xe0 Quimper avec Thibault et tout le monde pour la journ\xe9e.\n"
    )
    assert set(found(french)) == {"Wildix", "Datto", "Quimper", "Thibault"}
    spanish = (
        "Hola Dana:\n\nGracias por la llamada de ayer. La migraci\xf3n ser\xe1 en Zaragoza.\n"
        "El programa se queda en el servidor de siempre hasta que nos llegue la licencia de\n"
        "Contaplus, que es la que falta.\nUn saludo,\nDirector de Sistemas\n"
    )
    assert set(found(spanish)) == {"Zaragoza"}


def test_another_alphabet_hides_nothing_and_adds_nothing():
    for before, after in ((0x306F, 0x3092), (0x0646, 0x0627), (0x4E2D, 0x6587)):
        glued = f"{chr(before)}ConnectWise{chr(after)}"
        assert found(f"{glued} {chr(before)} {chr(after)}") == ["ConnectWise"]


def test_typography_is_read_as_typed():
    for code in (0x2018, 0x2019, 0x02BC, 0x00B4):
        assert found(f"From: Siobhan O{chr(code)}Brien") == ["Siobhan O'Brien"]
    for code in (0x201C, 0x201D, 0x201E, 0x00AB, 0x00BB):
        quoted = f"{chr(code)}Proxmox{chr(code)}"
        assert found(f"They call it {quoted} in the office there.") == ["Proxmox"]
    for code in (0x2010, 0x2011):
        assert found(f"From: Jean{chr(code)}Luc Moreau") == ["Jean-Luc Moreau"]
    for code in (0x2012, 0x2013, 0x2014, 0x2022, 0x00B7, 0x25CF, 0x25AA):
        text = f"We asked the people at Proxmox{chr(code)}and those at Datto{chr(code)}Veeam too."
        assert set(found(text)) == {"Proxmox", "Datto", "Veeam"}
    for code in (0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF):
        assert found(f"We asked the people at Prox{chr(code)}mox about it.") == ["Proxmox"]
    for code in (0x00A0, 0x202F, 0x3000, 0x000B):
        assert found(f"We asked Dana{chr(code)}Larkin about it all.") == ["Dana Larkin"]
    assert found(f"We asked Datto{chr(0x2026)} and they said no.") == ["Datto"]


# Whole documents ----------------------------------------------------------------------

THREAD = """From: "Nair, Priya" <priya.nair@example.com>
To: Dana Lee <dana@example.com>, Marcus Ruiz <marcus@example.com>
Cc: "Kowalczyk, Tomasz (Fabrikam)" <tomasz@example.com>, Contoso Service Desk <help@example.com>
Date: Mon, 23 Nov 2026 10:41:58 -0500
Subject: RE: [EXTERNAL] Northwind cutover Friday - Proxmox hosts and the Datto

CAUTION: This email originated from outside of the organization.

Hi Dana,

Thanks for the quick call. A few notes before Friday:

1. Tomasz from Fabrikam will be on site in Tacoma from 5 PM.
2. The Proxmox hosts are patched. Please don't touch the pool until the Datto is done.
3. Our controller, Anneliese Vogt, needs Sage Intacct back by 7 AM Saturday.
4. The admin password for the old NAS changed last week:
   TigerLily is the new one, I'll text you the rest.
5. Can ConnectWise show the change ticket to our side? Lucia couldn't see #4512093.

If the Meraki drops again we should swap it for the FortiGate we quoted. The MX84 is out of
support anyway.

Best regards,

Priya Nair
Director of Infrastructure
Northwind Traders
Office: (253) 555-0142 | Mobile: (253) 555-0177
www.northwind.example.com

> On Fri, Nov 20, 2026 at 4:02 PM Dana Lee <dana@example.com> wrote:
>
> Good morning Priya, Marcus will drive the Proxmox side and Grace Okafor will tell the
> Woodgrove auditors that the VPN is down.
>
> Sent from my iPhone
"""

PLAN = """# Project Plan: Northwind Traders Infrastructure Refresh

**Client:** Northwind Traders (Tacoma and Spokane)
**Project manager:** Dana Lee

## 1. Executive Summary

Northwind runs its warehouse on servers that are out of warranty. This project replaces the two
VMware hosts with a Proxmox cluster and moves the mailboxes to M365.

### In Scope

- Replace the HPE ProLiant hosts with Dell PowerEdge nodes
- Roll out SentinelOne and Huntress through ConnectWise
- Out of scope: the Kronos time clocks

| Milestone | Owner | Target Date | Status |
|---|---|---|---|
| Kickoff meeting | Dana Lee | October 6 | Complete |
| Hardware received | Tomasz Kowalczyk | October 23 | Complete |

## 2. Risks

1. **Fishbowl licensing.** Kwame Mensah has asked Fishbowl support for a transfer.
2. **Password vault.** Keeper will hold all of them.
3. Weather can close the pass.

Approved by: ____________________
"""


def test_an_email_thread():
    offered = found(THREAD)
    assert offered[:3] == ["Priya Nair", "Dana Lee", "Marcus Ruiz"]
    assert set(offered) == {
        "Dana Lee", "Priya Nair", "Marcus Ruiz", "Tomasz Kowalczyk", "Northwind", "Proxmox",
        "Datto", "Fabrikam", "Tacoma", "Anneliese Vogt", "Sage Intacct", "ConnectWise", "Lucia",
        "Meraki", "FortiGate", "MX84", "Grace Okafor", "Woodgrove", "Contoso",
    }  # fmt: skip
    assert counts(THREAD)["Proxmox"] == 3 and counts(THREAD)["Dana Lee"] == 2


def test_a_project_plan():
    assert set(found(PLAN)) == {
        "Dana Lee", "Northwind", "Tacoma", "Spokane", "VMware", "Proxmox", "M365", "ProLiant",
        "PowerEdge", "SentinelOne", "Huntress", "ConnectWise", "Kronos", "Tomasz Kowalczyk",
        "Fishbowl", "Kwame Mensah",
    }  # fmt: skip


def test_the_same_text_always_gives_the_same():
    assert terms.found_in(THREAD + PLAN, []) == terms.found_in(THREAD + PLAN, [])


# Odd input ----------------------------------------------------------------------------


def test_odd_input_is_no_trouble():
    noise = random.Random(7)
    lines = "\n".join(base64.b64encode(noise.randbytes(57)).decode() for _ in range(200))
    dump = "\n".join(f"{n:08x}  {noise.randbytes(16).hex(' ')}" for n in range(200))
    for text in (
        "",
        " \n\t\n",
        "\x00\x01\x02",
        "||||\n||||\n|a|\n",
        ",,,\n,,,\n,,,\n",
        lines,
        dump,
    ):
        assert found(text) == []
    json = '{"owner": "Dana Lee", "site": "Alpine Ski House", "deviceName": "WS-0142"}'
    assert set(found(json)) == {"Dana Lee", "Alpine Ski House"}
    log = (
        "2026-11-22 23:00:01.114 INFO [Scheduler] Starting job 'Northwind-Nightly' (id 7f3c2b1a)\n"
        "2026-11-22 23:00:41.090 WARN [VssProxy] Writer 'SqlServerWriter' is in state 8 (Failed)\n"
        "   at System.IO.FileStream.Init(String path, FileMode mode) in C:\\build\\Indexer.cs:4\n"
        "2026-11-23 00:10:07.456 ERROR [Replica] Cannot connect to the Proxmox API on 10.20.30.41\n"
    )
    assert found(log) == ["Proxmox", "Northwind"]


@pytest.mark.parametrize(
    "unit",
    [
        "A ", "Aa ", "aA", "aa Aa, ", "Aa & Bb ", "a,b,c\n", "Aa|Bb|Cc\n", "(", "<", "@", "a_",
        "a:", "key ", "Dana Lee <dana@example.com>, ", "Zebra ZD421 ", "\x0b", "-", ":",
    ],
)  # fmt: skip
def test_no_input_takes_long(unit: str):
    for text in (unit * (20_000 // len(unit)), "From: " + unit * (20_000 // len(unit))):
        started = time.perf_counter()
        terms.found_in(text, [])
        assert time.perf_counter() - started < 3  # under half a second on a busy laptop


def test_text_of_any_kind_is_looked_through():
    # "I do": an initial, then what's a particle in a Portuguese name.
    assert found("I do know the people at Fabrikam, as I do not.") == ["Fabrikam"]
    assert found("What do I\nI de la\nA. da B.\nDr.\nMr. O\n& Sons\nde la Cruz\n") == ["Cruz"]
    pieces = (
        "I do|I|do|de la|von|A.|B|O'|Dr.|Mr.|Mrs. |Dana Lee|Maria|Cruz|Fabrikam|ConnectWise|MX84|"
        "ZD421|GmbH|Inc.|& Co|und|der|die|das|mit|the|of|at|password|key|key:|From:|To:|Owner:|"
        "First Name,Last Name,Password|a,b,c|x|1|.|!|:|-|--|#|##|>|*|**|_|`|\"|'|(|)|[|]|{|}|<|>|"
        "&|@|=|/|\\|\t|;|,|, | & |\n|\n|\n\n|  |    |\xe9|\xdf|\u2019|\u2014|\x00|\x01"
    ).split("|")
    rng = random.Random(7)
    for _ in range(1500):
        text = "".join(rng.choice(pieces) + rng.choice(("", " ", " ")) for _ in range(40))
        for find in terms.found_in(text, []):
            assert vocabulary.usable(find["term"]) and find["count"] >= 1, (find, text)


def test_a_full_file_is_looked_through_at_once():
    text = (THREAD + PLAN + ASSETS + GERMAN) * 10
    started = time.perf_counter()
    assert len(terms.found_in(text[:20_000], [])) == terms.LIMIT
    assert time.perf_counter() - started < 1.5  # about 0.05 s


def test_everything_offered_fits_in_the_list():
    for text in (ASSETS, THREAD, PLAN, GERMAN):
        for term in found(text):
            assert vocabulary.usable(term) and not vocabulary.secretish(term)
            assert vocabulary.add_correction("", term, None) == term
    long = "Zorvexqualtrane Brindlewoodhurst Larkinfarnsworth"
    assert len(long) > terms.LONGEST and found(f"We asked the people at {long} about it.") == []


def test_the_everyday_words_are_written_as_terms_compares_them():
    lists = [everyday.WORDS, everyday.NAMES, everyday.SHORT, everyday.OTHER_LANGUAGES]
    for words in (*lists, everyday.CALENDAR, everyday.TAILS, everyday.ROLES):
        assert all(word == terms._fold(word) and word.isascii() for word in words)
    for words in lists:
        assert words.isdisjoint(STOP_WORDS)  # those count already


# Through the API ----------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ZACHCAP_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    return TestClient(create_app("test-token"), base_url=LOCAL)


def look(client: TestClient, path: Path):
    return client.post("/api/vocabulary/found", headers=AUTH, json={"path": str(path)})


def test_a_files_names_and_terms_are_offered(client: TestClient, tmp_path: Path):
    plan = tmp_path / "plan.md"
    plan.write_text("We moved Proxmox to Fabrikam with Priya Nair and Dana Lee.", encoding="utf-8")
    before = plan.read_bytes()
    client.put("/api/settings", headers=AUTH, json={"vocabulary": "proxmox: prox mocks"})
    store = Store()
    store.person_id("Dana Lee")  # named already: Whisper gets the name anyway
    store.close()

    answer = look(client, plan)

    assert answer.status_code == 200
    assert answer.json() == {
        "name": "plan.md",
        "terms": [{"term": "Priya Nair", "count": 1}, {"term": "Fabrikam", "count": 1}],
        "room": 4000 - len("proxmox: prox mocks"),
        "cut": False,
    }
    # Only read: the file, the list and the library are as they were.
    assert plan.read_bytes() == before
    settings = client.get("/api/settings", headers=AUTH).json()
    assert settings["vocabulary"] == "proxmox: prox mocks"
    store = Store()
    notes = store.db.execute("SELECT * FROM meeting_notes").fetchall()
    assert store.meetings() == [] and notes == []
    store.close()
    for path in (tmp_path / "home").rglob("*"):
        assert not path.is_file() or b"Fabrikam" not in path.read_bytes()


def test_an_exported_email_and_a_long_file(client: TestClient, tmp_path: Path):
    message = tmp_path / "thread.eml"
    message.write_bytes(
        b"From: =?utf-8?Q?Xim=C3=A9na_Iturbe?= <x@example.com>\r\nTo: Dana <dana@example.com>\r\n"
        b"Subject: The Datto\r\nContent-Type: text/plain\r\n\r\nWe asked the people at Litware.\r\n"
    )
    offered = look(client, message).json()["terms"]
    assert [find["term"] for find in offered] == ["Xim\xe9na Iturbe", "Dana", "Litware", "Datto"]
    long = tmp_path / "long.log"
    long.write_text("We asked the people at Litware. " + "x " * attachments.MAX_TEXT + " Datto")
    answer = look(client, long).json()
    assert answer["cut"] is True and [find["term"] for find in answer["terms"]] == ["Litware"]


def test_a_file_that_cant_be_read_says_why(client: TestClient, tmp_path: Path):
    picture = tmp_path / "screen.png"
    picture.write_bytes(b"\x89PNG")
    for path, why in (
        (picture, "screen.png isn't a text file ZachCap reads"),
        (tmp_path / "missing.txt", "Couldn't read missing.txt"),
    ):
        refused = look(client, path)
        assert refused.status_code == 400 and refused.json()["detail"].startswith(why)
    empty = tmp_path / "empty.txt"
    empty.write_text(" \n")
    assert look(client, empty).json()["detail"] == "empty.txt is empty"
    assert client.post("/api/vocabulary/found", json={"path": str(picture)}).status_code == 401


def test_the_ticked_ones_are_added_all_or_none(client: TestClient):
    def add(*ticked: str):
        return client.post("/api/vocabulary/add", headers=AUTH, json={"terms": list(ticked)})

    def saved() -> str:
        return client.get("/api/settings", headers=AUTH).json()["vocabulary"]

    assert add("Dana Lee", "ConnectWise").json() == {"vocabulary": "Dana Lee\nConnectWise"}
    assert add("connectwise", " Proxmox ", "Proxmox").json()["vocabulary"] == (
        "Dana Lee\nConnectWise\nProxmox"
    )
    refused = add("Datto", "C:Drive")
    assert refused.status_code == 400 and "colon" in refused.json()["detail"]
    assert saved() == "Dana Lee\nConnectWise\nProxmox"  # and not Datto either
    client.put("/api/settings", headers=AUTH, json={"vocabulary": "x" * 3980})
    full = add("Datto", "Fabrikam", "Northwind")
    assert full.status_code == 400 and "full" in full.json()["detail"]
    assert saved() == "x" * 3980
    assert add("Datto", "Litware").json()["vocabulary"] == "x" * 3980 + "\nDatto\nLitware"
    assert add().json()["vocabulary"] == saved()
    assert client.post("/api/vocabulary/add", json={"terms": ["Datto"]}).status_code == 401
