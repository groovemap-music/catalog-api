"""What the placeholder-label rule must decide, for the unit test and the real-engine test alike.

Every id and name is from the 2026-11-07 Lux catalog, read against its Discogs profile.
"""

PLACEHOLDER_LABEL_CASES: list[tuple[str, str, bool]] = [
    # The placeholder itself. Its graph node has no Discogs row, only the name from release references.
    ("1818", "Not On Label", True),
    # Discogs' per-artist holding areas, including an artist whose name is a number.
    ("147757", "Not On Label (2Pac)", True),
    ("2961239", "Not On Label (311)", True),
    ("1265119", "Not On label (Airbourne)", True),
    ("2034708", "Not on Label (6ix9ine)", True),
    ("4543890", "Not On Label(Daniel Shushan)", True),
    # The self-released family, as typed.
    ("3731000", "Not On Label ( Aley Sheer Self-released)", True),
    ("3731001", "NOT ON LABEL (Sora (10) Self-released)", True),
    ("3731002", "Not On Label ( Dreamlore Self-realesed)", True),
    ("3927573", "Not On Label [Djet-X Self-Released]", True),
    ("4085511", "Not On Label {Torp (2) Self-Released}", True),
    ("4198687", "Not On Label(Daniel Shushan Self Released)", True),
    ("3819679", "Not On Label Kjwan Self-released", True),
    ("4433386", "Not On Labele (Siluett Self-Released)", True),
    ("3764371", "(Not On Label (La Bedoune Self-released)", True),
    ("4363249", "(Not On Label) (Héctor Filio Self-released)", True),
    ("4184344", "Label: Not On Label (Pedro Vasconcellos Self-released)", True),
    ("4462375", "Use Not On Label (Primate House Self-released)", True),
    # Real labels that look like placeholders. A disambiguated "Not On Label" is a real netlabel.
    ("109707", "Not On Label (2)", False),
    ("2614996", "Not On Label (3)", False),
    ("490076", "Not On Label Records", False),
    ("1605829", "Not On Label Records (2)", False),
    ("4078377", "Not On Label Releases", False),
    ("1395819", "Not On LabelA", False),
    ("4470868", "Big Beat Records (Not On Label)", False),
    # Named like "no label", and real: Discogs says so on several of them.
    ("100895", "No Label (5)", False),
    ("71595", "White Label (5)", False),
    ("18027", "Unknown", False),
    ("273924", "Unknown Label (2)", False),
    ("20019", "Bootleg", False),
    ("9508", "Promo", False),
    ("41171", "Blank", False),
    ("69362", "Test Pressing", False),
    ("89978", "Non Label", False),
    ("359667", "Unsigned (3)", False),
    ("412368", "Nolabel (2)", False),
    ("1866", "Columbia", False),
]
