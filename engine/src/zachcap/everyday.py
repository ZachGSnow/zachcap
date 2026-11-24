"""The words terms.py goes by.

Most are too ordinary to be worth a place in Words to know: Whisper spells them right by itself.
Names are told from ordinary words by how a file writes them, and these decide where that says
nothing: a word that starts a line, a heading or a table cell, or that a form capitalizes. They
are written without accents and in their plain forms: terms.py knows the plurals and the verb
forms, and what related.STOP_WORDS has isn't repeated.
"""

# Office, trade and IT words.
WORDS = frozenset(
    """
    abort absent absolute abstract accent accept access accessory accident accidental accompany
    account accountant accurate acknowledge acquire action activate active actual adapt adapter
    addendum additional address adjacent adjust adjuster admin administer administrator admit
    advantage advice advise adviser advisor advisory affiliate age agency agenda agent aggregate
    agree aid aim air airport aisle alarm alcohol alert algorithm alias align alive allocate allow
    alpha alter alternate alternative amount an analog analysis analyst analytics angle annex
    anniversary annotate announce annual antenna antivirus apart apologize apology app appeal appear
    appendix applicant appoint approach appropriate approval approve approximate arch architect
    architecture archive area arm arrange array arrival article artificial as ascii assemble
    assembly assess asset assign assist assistant associate assume assure async at attach attempt
    attend attendee attn attorney attribute audio audit auditor authentic authenticate author
    authorize auto automate automatic automotive ave avenue average avoid await award aware awful
    backend background backlog backorder backup backward badge ban bandwidth bank banner bar barcode
    base baseline basic basis batch battery bay bcc be become begin behave behavior bell benchmark
    beneficiary benefit best beta bid bill bin binary bind binder blank block blog blue blueprint
    blvd board body book bookkeeper booklet bookmark boolean boost boot booth border boss bottleneck
    bought bound boundary box bracket brain branch brand brave breach break breakdown breakout
    bridge brief bright broadband broadcast brochure broke broken broker brother brought brown
    browse browser bucket budget buffer bug build builder built bulk bulletin bump bundle bureau bus
    busy button buy buyer by bypass byte cabinet cable cache cafe cafeteria calculate calculator
    calendar calibrate caller camera campaign campus cancel candidate cap capital capture car carbon
    card cardboard care career cargo carrier carry cart carton cartridge case cash cashier cast
    catalog catch category cause caveat cc cease ceiling cell cellular cement center central century
    ceremony certain certificate certify chain chair chairman challenge champion changelog channel
    chapter character charge charger chart charter chase chassis chat checklist checkout checkpoint
    cheer chief child children chip choice choose chose chosen chunk circuit cite citizen city civic
    civil claim clarify class classic classifier classify classroom clause clay clean cleanup clerk
    click clinic clip clipboard clock clone closer closet closure cloud club cluster coach code
    codec collaborate colleague collect collective collector college color colour column combine
    comma command commerce commercial commit committee common communicate companion company compare
    comparison compensate competitor compile complaint complete complex comply component compose
    comprehensive compress compute computer concept concern conclude concrete conduct conductor
    confidential configure confirm conflict confuse congress connect connector consent consider
    consistent console consolidate constant constraint construct consult consultant consume consumer
    contact contain container content context contingency continue contract contractor contractual
    contrary contribute contributor control controller convene convenient convert cook cookie
    coordinate coordinator copier copy copyright cord core corner corporate correct correspond
    corridor cost council counsel count counter country county course court courtesy cover coverage
    crash create creative creator credential credit creditor crew crisis criteria criterion cross
    crucial cruise cube cubicle culture cup currency current curriculum cursor custom customize cut
    cutoff cutover cyber cycle daily damage danger dark dash dashboard data database datacenter
    datasheet datastore date deadline deal dealer dean dear debit debt debug decide decimal deck
    declare decline decode decrease deduct deeper default defect defendant defense defer deficit
    define degree delay delegate delete deliver delivery demand demo demographic demonstrate denial
    deny departure depend dependency dependent deploy deposit depot dept depth deputy derive desc
    describe design designate designer desk desktop despite destroy detach detail detect detector
    determine develop developer device diagnose diagnosis diagnostic diagram dial dialog dictionary
    die differ difficult digest digital dir direct directive director directory disaster disc
    discard discharge disclaimer disclose disclosure disconnect discontinue discount discover
    discovery discuss disk dispatch dispatcher display disposal dispose dispute distribute
    distributor district dividend do doc dock doctor dollar domain domestic donate donor door double
    downgrade download downstream downtime downtown draft drama draw drawer drill drive driver
    drywall dual duct due dummy duplex duplicate duty dynamic earlier earth ease easier east eastern
    economic economy ecosystem edit editor educate effect effective efficiency efficient effort egg
    elaborate elect electric electrician electronic elevator elsewhere embed emergency emit employee
    employer empty enable enclosure encode encourage encrypt endorse endpoint energy enforce engage
    engine engineer enjoy enrich enroll ensure enter enterprise entire entrepreneur entry envelope
    environmental episode equal equip equity equivalent error escalate escape escrow especially
    establish estate estimate estimator etc ethic evaluate event evil exact exam examine example
    exceed excellent excess exclude exclusive excuse execute executive exempt exercise exhibit exist
    exit expand expect expedite expense expensive experimental expert expertise expire expiry
    explain explicit exploit explore explorer export exporter expose exposure express ext extend
    extensive extent external extinguisher extra extract extreme fabric face facilitate factor
    factory faculty fail failover failure fallback false familiar fan fantastic fast faster fastest
    fatal father fault favorite fax feature federal fee feed feedback feeder festival fetch fiber
    field fight filename fill filter final financial find finish finite fire firewall firm firmware
    fiscal fit fix fixture flag flash flat fleet flex flight float floor flow flower focus folder
    follow font food footage footer footnote footprint force forecast foreign forest forget fork
    form formal format formula forum forward foster founder frame framework franchise fraud free
    freeze freezer freight frequency frequent fresh fridge friendly frontend frozen fruit fuel
    fulfill functional fund fundamental furniture furthermore fuse fusion future fwd fyi gallery gap
    garage garbage gas gasket gate gateway gather gauge gear gender generate generator generic gift
    gigabit glass global glossary glue go goal gold golden govern governor grab grade graduate grant
    graph graphic greater greatest green greet grid grocery gross ground group grow growth guarantee
    guard guardian guest guide guideline gun gym hack hall hallway halt hand handbook handle handler
    handoff handover handset handy hardware harsh hash hat hate hazard he head header headline
    headphone headquarters headset health healthcare healthy hear heartbeat heat heater height hell
    helpdesk helper hence hereby heritage hi hide hierarchy higher highlight highway hint hire
    history hit holder holiday home homepage homework honor hook horizontal hospital host hostname
    hot hotel hotfix hotspot house household hub human hwy hybrid hypervisor hypothesis icon id
    ideal identifier identify idle if ignore ill illegal image imagine immediate impact imply import
    improve impulse in inactive inbound inbox incentive inch incident incl include income incomplete
    incorporate incorrect increase incremental incur indeed independent index indicate indicator
    indirect individual indoor industrial industry info inform informal infrastructure inherit
    initial initialize initiate initiative ink input inquire inquiry insert inside insight inspect
    inspector inspire install installer instant institute instruct instructor insure insurer intake
    integer integrate intelligent intend interact interactive intercept interchange interest
    interface interior intermediate intermittent internal international interpret interrupt interval
    interview intranet intro introduce invalid inventory invest investigate investor invite invoice
    involve iron is isolate issuer it italic item iterate itinerary jam jar job join joint journal
    jump junior justice justify key keyboard keyword kickoff king kiosk kit kitchen knowledge lab
    label labor laboratory land landline landlord landscape lane language lap laptop large laser
    latency lateral latest latter launch law lawsuit lawyer lay layer layout lazy lead leader leak
    lean learn lease lecture ledger left legacy legal lender length lens lesson letter level lever
    leverage liable liaison library license licensee licensor lien life lifecycle lift light
    lightning lightweight likewise limit linear link liquid list literal literature live load loader
    loan loaner lobby local locate lock locker lodge log logistics logo lookup loop lose loss lost
    lounge lower loyalty luck machine machinery macro magic magnetic mail mailbox main mainframe
    maintain maintainer major maker malware manage manager mandate mandatory manual manufacture
    manufacturer map margin marine mark marker market marketplace markup mask master match material
    math mathematics matrix mature max maximum mayor me meal meanwhile measure mechanic media
    mediate medium meet megabit member memo memorandum memorial memory mental mentor menu
    merchandise merchant mere merge merger mesh message metadata metal meter method methodology
    metric metro mezzanine mgmt mgr microphone microwave middle midnight migrate mile milestone
    military min mineral minimal minimize minimum minister ministry minor mirror misc mismatch
    mistake mitigate mix mixture mobile mode model modem moderate modern modifier modify modular
    module mom money monitor moral moreover mortgage motion motivate motor mount mountain mouse
    multimedia multiple municipal museum music musician mutual my narrative nation national native
    natural nature navigate near nearby nearest neat necessary negative negotiate neighbor
    neighborhood nest net network neutral newer newest newline newsletter newspaper no node noon
    normal north northeast northern northwest nose note notebook notify nuclear null numeric nurse
    nutshell oak object objective observe obsolete obstacle obtain occupancy ocean of offense offer
    office officer official offline offset offshore offsite oh oil ok older omit on onboard ongoing
    online onsite opaque open operate operational operator opinion opponent opposite opt optimal
    optimize optional or oral order ordinary org organic organize organizer orient origin original
    outage outbound outcome outdoor outer outgoing outgrown outlet outline output outside outsource
    outstanding overall overdue overflow overhead overlap overnight override oversee overtime
    overview owe owner pace pack package packet pad page paid pain paint pair panel paper paperwork
    paragraph parallel parameter parcel parent park parse partial participant particular partner
    party passkey past paste patch path patient patrol pay payroll peer pen penalty pending percent
    percentage perform perimeter period periodic peripheral permanent permit personal personnel
    perspective pharmacy phase philosophy phishing phone photo photocopy photograph photographer
    physician physics pick pickup picture pie pilot ping pipe pipeline pitch pixel pkg placeholder
    plain planner plant plastic platform playbook player plaza plot pls plug plugin plumbing poetry
    pointer police policy politics poll pool popular portal portfolio portrait posix post postage
    postal poster postpone potential power practice practitioner preamble preface prefer prefix
    preliminary premise premium prepare prerequisite prescribe present presenter preserve president
    press pressure prevent preview price primary prime principal principle print printer prior
    privacy private privilege proactive probe procedure proceed process processor procure produce
    producer product professional professor profile profit program programmatic programmer progress
    prohibit project projector prominent promise promote prompt proof propagate proper property
    proposal propose proprietary prospect protect protective protocol prototype prove provide
    provider province proxy public publish publisher pull pulse pump purchase purchaser pure purpose
    pursuant push qty quad qualify quarantine quarter query questionnaire queue quirk quit quota
    quote rack radar radio rail railway raise ram random range rank rapid rate ratio raw rd react
    read reader realize reassign reboot rebuild recall receipt receive receiver recent receptionist
    recipe recipient recognize recommend reconcile reconnect record recorder recover recovery
    recruit recruiter recur recycle red redeem redirect reduce redundancy redundant ref refer
    referral refine reflect reform refresh refrigerator refund refurbished refuse reg regard region
    regional register registrar registry regret regular regulate regulator regulatory rehearsal
    reimburse reinforce reject relate relative relax relay release relevant relief reload relocate
    rely remain remainder remark remediate remedy reminder remote removal remove rename render renew
    renewal rent rental reorder rep repair repeat repeater replace replica replicate repo report
    reporter repository represent representative reproduce republic req requester require resale
    rescue research researcher reseller reserve reset reside resident residential resign resilient
    resist resistant resolve resort resource respect respective respond respondent response
    responsive rest restart restaurant restore restrict result resume retail retailer retain
    retainer retire retrieve retry return reuse reveal revenue reverse review reviewer revise revoke
    reward rewrite rice rich ridge rise risk risky road roadblock roadmap robot robust role roll
    rollback rollout rollover roof room root roster rotate rough round route router routine row rule
    runbook runtime rural safe safeguard safety saint salary sale sample sandbox satellite satisfy
    save scale scan scanner scenario scene schedule scheduler schema scheme school scientific
    scientist scope score scrap scratch screen screenshot script scroll seal search season seasonal
    seat sec secondary secrecy secretary sector secure seek segregate select self sell seller
    semester seminar senior sensitive sensor separate serial series serve server service settle
    setup severe shade shadow shape share shareholder sharp sheet shelf shell shelter shield shift
    ship shoe shop short shortage shortcut shorter shred shut shutdown shutter sick sidebar sign
    signage signal signature significant signoff silent silly silver similar simplify simply
    simulate site situate size sketch skill skip slice slide slight slot slow slower smaller
    smallest smart smartphone smooth snap snapshot snow so social society sock socket soft software
    solar sold solid solo solve son sound source south southeast southern southwest space spam spare
    spark speak speaker special specialist specialize specialty specific specify spectrum speech
    speed spell spend spent split sponsor spot spotlight spray spread spreadsheet sprint square
    squeeze stable stack staff stage stakeholder stamp stand standalone standard standby star
    startup state static statistic status statute stay ste steel sticker stock stolen stone storage
    store story straight strategic strategy stream street strength strengthen stress strict strike
    string strip strong structural structure student studio study style subcontractor subj subject
    submit subnet subordinate subscribe subscriber subsequent subset subsidiary subsidy substantial
    substitute subtotal succeed success successor sufficient sugar suggest suit suite sum summarize
    summary summit supervise supervisor supplier supply support supporter suppose suppress surcharge
    surface surge surgery surplus surround survey suspect suspend switch symbol symptom sync
    synchronize syndicate synopsis synthetic system systematic tab table tablet tag takeaway talent
    tank tap tape target tariff task tax taxpayer tbd teach teacher teammate tech technician
    technique technology tel telecom telephone telephony teller temp temperature template temple
    temporary tenancy tenant tend tender term terminal terminate terminology territory tester
    testimony text textbook theater theft theme theory therapy therefore thermal thermostat thread
    threat threaten threshold throat throttle throughput throw thru thumb tick tier tile timeframe
    timeline timeout timer timesheet timestamp timezone tiny tip tire tissue title to tobacco toc
    toggle toilet tolerate toner tool toolbar toolbox toolkit top topic topology total touch
    touchscreen tour tourist tower town toy trace traceback track tracker tractor trade trademark
    trader traditional traffic trail trailer train trainee trainer trait transcript transfer
    transform transit translate transmit transparency transparent transport trash travel traveler
    tray treasurer treasury treat treaty tree trend triage trial tribe trigger trim trouble
    troubleshoot truck true trunk trust trustee truth tube tune tunnel turnaround turnover tutor
    tutorial tv twice txt type typo unable unassigned uncertain unchanged underground underlying
    undertake underwrite undo unexpected unfortunate unicode uniform union unique unit unity
    universal unknown unlike unlimited unlock unmanaged unnecessary unpaid unplanned unplug
    unrelated unsafe unsubscribe unsupported unused unusual unwanted up upcoming upgrade uplink
    upload upper upstairs upstream uptime upward urban urgency urgent us usable usage useful usual
    utilize vacancy vacant vacuum valid validate value valve van variant variety vary vast vault
    vector vehicle vending vendor vent venture venue verbose verify versus veteran viable vice video
    view viewer viewpoint violate virtual virus vision visit visitor visual vital vocabulary voice
    voicemail void voltage volume voluntary volunteer vote voucher vs wage waist waive waiver
    walkthrough wall wallet wallpaper war warehouse warm warn warrant warranty waste watch water we
    wealth web webcam webinar webpage website weekday weigh weight weird welcome welfare west
    western wheel whereby whereof whisper white whiteboard wholesale wide widespread width wiki wild
    winner wire wise withdraw withdrawal wood workbook worker workflow workforce workload workplace
    worksheet workshop workspace world worldwide worth wrap wrapper writer written xray yard yellow
    zone
    """.split()
)

# Names everyone knows: brands, countries, states, cities.
NAMES = frozenset(
    """
    adobe africa african alabama alaska alexa alphabet amazon amd america american americas
    amsterdam android angeles apple arizona arkansas asia asian athens atlanta atlantic austin
    australia australian austria austrian aws azure baltimore barcelona beijing belgian belgium
    berlin bing birmingham bluetooth boston brazil brazilian britain british brooklyn brussels
    budapest bulgaria california canada canadian canon carolina charlotte chatgpt chicago chile
    china chinese chrome cincinnati cisco citrix claude cleveland colombia colorado connecticut
    copenhagen copilot cortana croatia cuba czech dakota dallas danish delaware delhi dell denmark
    denver detroit diego dubai dublin dutch ebay edge egypt egyptian england english epson estonia
    ethernet europe european excel exchange facebook fedex finland finnish firefox florida france
    francisco frankfurt french geneva georgia german germany gmail google greece greek greenland
    hamburg hampshire hawaii helsinki houston hp hpe hulu hungarian hungary ibm iceland icloud idaho
    illinois imac india indian indiana indonesia instagram intel internet ios iowa ipad ipados
    iphone ipod iran iraq ireland irish israel israeli istanbul italian italy jamaica japan japanese
    java javascript jersey jerusalem kansas kentucky kenya kindle korea korean latin latvia lebanon
    linkedin linux lisbon lithuania london louisiana luis luxembourg mac macbook macos macs madrid
    maine malaysia malta manchester manhattan maryland massachusetts mastercard messenger mexican
    mexico miami michigan microsoft midwest milan milwaukee minneapolis minnesota mississippi
    missouri montana montreal morocco moscow mumbai munich nashville nebraska netflix netherlands
    nevada nigeria norway norwegian nvidia oakland ohio oklahoma onenote ontario openai oracle
    oregon orlando osaka oslo outlook pacific pakistan panama paris paypal pennsylvania peru
    philadelphia philippines phoenix photoshop pinterest pittsburgh playstation poland portland
    portugal portuguese powerpoint prague python quebec reddit rhode romania rome russia russian
    sacramento safari samsung saudi scotland scottish seattle serbia shanghai singapore siri skype
    slovakia slovenia snapchat sony spain spanish spotify stockholm sweden swedish swiss switzerland
    sydney taiwan tampa teams tennessee tesla texas thailand tiktok tokyo toronto tucson turkish
    twitter uber ukraine ukrainian unix ups usa usps utah vancouver vegas venezuela vermont vienna
    vietnam virginia visa wales warsaw washington welsh whatsapp wifi wikipedia windows wisconsin
    wordpress wyoming xbox xerox yahoo york yorker youtube zealand zoom zurich
    """.split()
)

# Days and months, holidays, time zones, titles, and what follows a company's name.
CALENDAR = frozenset(
    """
    ag am apr april aug august autumn bst bv cdt cet christmas co corp cst dec december dr easter
    edt esq est eve fall feb february fri friday gmbh gmt god halloween hanukkah ii iii inc iv ix
    jan january jesus jr jul july jun june kg llc llp lp ltd madam mar march may mdt miss mon monday
    mr mrs ms mst nov november nv oct october ohg pdt plc pm prof pst pty ramadan sa sas sat
    saturday sep sept september sir spa spring sr srl summer sun sunday thanksgiving thu thur thurs
    thursday tue tues tuesday ug utc vi vii viii wed wednesday winter xi xii
    """.split()
)

# Abbreviations any office or IT document is full of.
SHORT = frozenset(
    """
    aa ab ac ach acl aes ai ak aka al ap api apis apps ar arp asap asn atm atms ats aud av az b2b
    b2c bc bgp bios bsod byod ca cad cam ccna cdn ceo cfo chf cidr cio ciso cli cmd cmo cms cpa cpu
    cpus crm csat csr css csv ct cto cups cve dc dcs ddos de dep dev dhcp dim dkim dll dlp dmarc dmz
    dns dob dos dpi dsl dvd dvr eal edi eft eod eol eom eos eoy erp eta etl eu eula eur exe faq faqs
    fifo fl fqdn fte ftp fy ga gb gbp gbps gdpr gg ghz gis gnu gpo gps gpu gui guid ha hba hdd hdmi
    hex hipaa hq hr html http hvac hz ia iaas iam iban icmp idf ids ie ieee iis il imap io iot ip
    ips ipsec iq irs iscsi isdn iso isp itar ivr jpeg jpg jpy json kb kbps kpi kpis ks kvm kw ky la
    lan lcd ldap led lldp lte lun ma mb mbps mbr md mdf mdm mfa mhz mi mib mms mn mo mpls msi msp
    msps mt mtu mx na nac nas nat nc nd nda ndas ne nfc nfs ngfw nh nic nics nj nm noc ntfs ntlm ntp
    nvr ny oem ooo os osha ospf oss otp ou pa paas pc pci pcs pdf pdfs pdo pdu php pii pin png po
    poc poe pop pos pptp pr ps psk pto ptr qa qbr qc qos qr raid rca rdp rds readme rfc rfi rfid rfp
    rfq ri rj rma rmm roi rpc rpm rpo rsa rss rsvp rto saas sam saml san sata sc sccm scp sd sdk seo
    sfp sftp sid siem sim sip sku skus sla slas smb sme smes sms smtp snmp soap soc sop sops sow sox
    spf sql ssd ssds ssh ssid ssids ssl ssn sso svg swift syslog tb tco tcp tftp tls tn tpm ttl tvs
    tx ua udp ui uk unc url urls usb usd usv ut utm utp uuid ux va vdi vga vip vlan vlans vm vms vnc
    voip vp vpn vpns vps vrf vss vt wa waf wan wap wep wi wlan wmi wpa wsus wv wy xls xlsx xml xss
    yaml yoy ytd zip
    """.split()
)

# Greetings, titles and labels in the other languages, and German office nouns.
OTHER_LANGUAGES = frozenset(
    """
    abend abnahme abschluss absprache abteilung adjunto adresse alle alles anbieter anderung anfang
    anforderung anfrage angabe angebot anhang anlage anleitung anmeldung anruf ansprechpartner
    antwort anwender anwendung anzahl arbeit arbeitsplatz art artikel asunto atenciosamente
    atentamente aufgabe aufgaben auftrag ausfall ausgabe auswahl bedankt bedarf bedingung beginn
    beispiel benutzer bereich bericht beschreibung besprechung beste bestellung besten besuch
    betreff betreft betrieb bild bitte boa bom bonjour bonsoir buchhaltung buenas bueno buenos
    buonasera buongiorno buro cara caro chef cher chere chers ciao cordialement cordiali dag damen
    dank datei dateien daten datenbank datenschutz datensicherung datum dauer dezember dia dias
    dienstag directeur diretor direttore dokument dokumente donnerstag drucker einfuhrung einkauf
    einladung einrichtung empfang ende entscheidung entwurf erfahrung ergebnis ergebnisse estimada
    estimadas estimado estimados februar fehler ferien fertigstellung firma folge frage fragen frau
    freigabe freitag freundliche freundlichen frist garantie geachte gebaude gebuhr geehrte geehrter
    gentile gerat gerate gesamt gesellschaft gesprach gesundheit gracias grazie groet groeten grund
    grundlage gruppe gruss grusse grussen gute guten guter hallo haus hause herbst herr herren herrn
    hersteller hilfe hinweis hinweise hola information informationen inhalt installation jahr jahre
    januar juli juni kapitel kauf keller kennwort kollege kollegen kollegin kolleginnen konto
    kontrolle kopie kosten kunde kunden lager lagerung leistung leiter leiterin leitung liebe lieber
    lieferant lieferung liste losung madame mademoiselle mai mangel mappe markt marz maschine
    massnahme massnahmen meldung meneer menge merci mevrouw miete minuten mit mitarbeiter
    mitarbeiterin mittag mitte mittwoch monat monate monsieur montag morgen muchas nachricht namen
    netz netzwerk neue neuen neuer neues nummer objet obrigada obrigado oggetto oktober ola
    onderwerp ordner ort papier para passwort pause planung platz preis preise prezada prezado
    probleme produkt projekt protokoll prozess prufung punkt punkte qualitat querida querido rabatt
    raum rechenzentrum rechner rechnung rechnungen rechte regel reihe reise responsabile responsable
    reunion riunione rolle ruckfrage ruckfragen ruckmeldung sache saludos salut saluti samstag
    schaden schritt schritte schule sehr seite seiten sekunde senhor senhora senor senora sicherheit
    sicherung signor signora sitzung sommer sonntag spalte sra srta standort standorte stelle steuer
    storung strasse stunde stunden summe systeme tage tardes teil teilnehmer teilnehmerin telefon
    telefonanlage termin termine thema themen titel tur ubergabe ubersicht ubertragung uhr umsetzung
    umstellung umzug unterlagen unternehmen unterschrift unterstutzung urlaub ursache verantwortlich
    verbesserung verbindung verfugung vergleich verkabelung verkauf verlauf vermieter version
    vertrag vertrage vertrieb verwaltung viele vielen vorgang vorlage vorschlag vorteil wartung
    wechsel weg weihnachten weise werk woche wochen wochenende wohnung wunsch zahl zahlung zeit
    zeitplan ziel ziele zimmer zugang zugriff zusammenarbeit zusammenfassung zustand zustimmung
    zweck
    """.split()
)

# After a name, what it's about rather than part of it (the Proxmox Cluster).
TAILS = frozenset(
    """
    account admin agent agreement app appliance backup backups branch call camera cameras client
    clients cluster computer computers connector contract controller controllers cutover dashboard
    database department deployment desk desktop desktops device devices division environment
    firewall hardware helpdesk host hosts hr infrastructure instance integration invoice issue it
    laptop laptops license licenses location machine machines meeting migration monitor monitoring
    monitors network office phone phones plan policy portal printer printers project proposal quote
    renewal report rollout router scanner scanners server servers service services site software
    staff support switch tablet tablets team tenant ticket training update upgrade user users
    workstation workstations
    """.split()
)

# In what an email header or a list of people has, a role or a place rather than a person.
ROLES = frozenset(
    """
    accounting accounts admin administrator alert alerts all backup backups billing calendar careers
    client compliance conference coordinator copier customer daemon department desk director
    engineer engineering events everyone external finance front group helpdesk hr human info
    internal invoices jobs lead legal list mailer main manager marketing monitoring newsletter none
    noreply notification notifications office officer open operations orders payable pool position
    postmaster president press printer receivable reception recipients reply resources room root
    sales scanner security service services spare specialist staff subsystem supervisor support
    system team technician training unassigned undisclosed unknown users vacant voicemail webmaster
    """.split()
)

# How an identifier from code or a log starts or ends (LastLogonTime, UserName).
IDENTIFIER_STARTS = frozenset(
    """
    create current default delete enable first for get has init is last max min num parse set show
    to update use
    """.split()
)

IDENTIFIER_ENDS = frozenset(
    """
    adapter address args at attribute builder by cache class code column config configuration
    connector context controller converter count created data date deleted disabled element enabled
    error event exception factory field file files flag flags for guid handler helper host id ids
    impl index info interval item items key length level limit list listener loader logger login
    logon manager map message mode modified name names node number object of offset on only option
    options parameter parameters parser path port property provider queue reader request response
    result results row runner seen service session settings size state status string table tag test
    time timeout timestamp type types updated uri url user util utils validator value values version
    writer
    """.split()
)
