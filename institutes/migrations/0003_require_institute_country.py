from django.db import migrations, models


ISO_ALPHA2_CODES = frozenset(
    """
    AD AE AF AG AI AL AM AO AQ AR AS AT AU AW AX AZ BA BB BD BE BF BG BH BI BJ BL BM BN BO BQ BR BS BT BV BW BY BZ
    CA CC CD CF CG CH CI CK CL CM CN CO CR CU CV CW CX CY CZ DE DJ DK DM DO DZ EC EE EG EH ER ES ET FI FJ FK FM FO FR
    GA GB GD GE GF GG GH GI GL GM GN GP GQ GR GS GT GU GW GY HK HM HN HR HT HU ID IE IL IM IN IO IQ IR IS IT JE JM JO
    JP KE KG KH KI KM KN KP KR KW KY KZ LA LB LC LI LK LR LS LT LU LV LY MA MC MD ME MF MG MH MK ML MM MN MO MP MQ MR
    MS MT MU MV MW MX MY MZ NA NC NE NF NG NI NL NO NP NR NU NZ OM PA PE PF PG PH PK PL PM PN PR PS PT PW PY QA RE RO
    RS RU RW SA SB SC SD SE SG SH SI SJ SK SL SM SN SO SR SS ST SV SX SY SZ TC TD TF TG TH TJ TK TL TM TN TO TR TT TV
    TW TZ UA UG UM US UY UZ VA VC VE VG VI VN VU WF WS YE YT ZA ZM ZW
    """.split()
)


def normalize_and_audit_institute_countries(apps, schema_editor):
    Institute = apps.get_model("institutes", "Institute")
    flagged = []
    for institute in Institute.objects.all().iterator():
        original = (institute.country or "").strip()
        country = original.upper()
        if country not in ISO_ALPHA2_CODES or country == "US":
            flagged.append((institute.pk, institute.name, original))
            country = "IN"
        if country != institute.country:
            Institute.objects.filter(pk=institute.pk).update(country=country)

    if flagged:
        print(
            "WARNING: Institute country could not be safely retained for these rows; "
            "backfilled to IN and requires business review:",
            flagged,
        )


class Migration(migrations.Migration):
    dependencies = [("institutes", "0002_institute_country")]

    operations = [
        migrations.RunPython(
            normalize_and_audit_institute_countries,
            migrations.RunPython.noop,
        ),
        migrations.AlterField(
            model_name="institute",
            name="country",
            field=models.CharField(max_length=2),
        ),
    ]
