# SPDX-License-Identifier: MIT
"""The widely used licences and every licence that 25 or more Fedora packages carry, each with a class, SPDX's OSI
and FSF flags and the SHA-256 of its text as SPDX publishes it, and the checks that hold a `LICENSES/<identifier>.txt`
and a declared licence to the table.

REUSE asks for a licence text copied verbatim, a holder line kept as the placeholder the published text carries, with
the copyright stated in `REUSE.toml` or a file's header (the REUSE FAQ, "Should I edit my license files?"); `reuse
lint` does not compare the text. Each entry's `digests` are every digest spdx/license-list-data published for
`text/<identifier>.txt` from the floor tag to the `master` branch `reuse download` fetches from, and `osi` and `fsf`
are that list's `isOsiApproved` and `isFsfLibre`; `packaging/license-text-digests.py` prints the entries. A text
whose identifier the table holds and whose bytes match none of its digests is reported under the
`license.text-modified` warning, and a text whose identifier the table does not hold under the `license.text-unpinned`
warning. `publisher.conf` `license_text_digests` names further `<identifier>:<sha256>` pairs, and a text whose
identifier it names is held to those digests too, so an unpinned text is compared as a pinned one is. A text that
differs warns rather than refuses, since format 1 sets passed without the comparison; a published revision of a text
joins the table as one more digest.

The class is this table's, since SPDX does not record such a property: `public-domain` a dedication, `permissive` a
licence whose conditions are notice and attribution, `weak-copyleft` one whose copyleft stops at the file or the
library, `copyleft` one whose copyleft reaches the whole work, and `limited-use` one that limits use by field, hosted
service, commercial use or derivatives, the source-available licences among them. `check-licenses` names each licence's
class in its summary line. A class is a reading of the licence's terms for an operator: whether a licence is used at all
is the allowlist's decision, which leaves every `limited-use` licence off its default, and the tools do not decide
whether two licences may be combined.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Set, Tuple

import asset_format as fmt
from findings import FindingCollector
from key_value_config import KeyValueDocument
from safe_read import RefusedRead, open_directory, read_file

PUBLIC_DOMAIN = "public-domain"
PERMISSIVE = "permissive"
WEAK_COPYLEFT = "weak-copyleft"
COPYLEFT = "copyleft"
LIMITED_USE = "limited-use"

DECLARED_DIGESTS_KEY = fmt.LICENSE_TEXT_DIGESTS_KEY
DECLARED_DIGEST_ITEM = re.compile(r"^(?P<identifier>[A-Za-z0-9][A-Za-z0-9.+-]*):(?P<digest>[0-9a-f]{64})$")


@dataclass(frozen=True)
class LicenseText:
    license_class: str
    osi: bool
    fsf: bool
    digests: Tuple[str, ...]


# Printed by `python3 packaging/license-text-digests.py --floor v3.20`; a digest tuple is newest first.
LICENSE_TEXTS: Dict[str, LicenseText] = {
    "0BSD": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("e3f18c71e10d673590eb9856c1d79dd3b4b0d65404efb5e8584dbede7edd608b",)),
    "AFL-3.0": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("de0612c5780d0d21f9982d2cadfd715d2d3b7a0dfeb532a5023d42a1d4fdce5c",)),
    "AGPL-3.0-only": LicenseText(COPYLEFT, osi=True, fsf=True, digests=("d8a6cc31abc16b6748c7a21f21611f5a1ec33f67d22ca23d7da1c19b95496bee",)),
    "AGPL-3.0-or-later": LicenseText(COPYLEFT, osi=True, fsf=True, digests=("d8a6cc31abc16b6748c7a21f21611f5a1ec33f67d22ca23d7da1c19b95496bee",)),
    "AMD-newlib": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("194934ff77c5c15488c3c3e23c9750aea0cb4a7286b3f53d2f71cdbd3247162f",)),
    "AML": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("5a18e36d6923eaefdb77cd01adaa4c823a0f33fc50071568e4a8f7017aaf8c84",)),
    "Apache-1.1": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("1521ffd9660fbf0e4648f72e2aa164fa91eb3eacee27fe36b732b95b3d64e28c", "37fa083ca6e83b20e2dea60ad9854bf4c89309f3a6a8a56153319d4c5c49d61d",)),
    "Apache-2.0": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("074e6e32c86a4c0ef8b3ed25b721ca23aca83df277cd88106ef7177c354615ff",)),
    "APSL-2.0": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("2d0c12edaaf8d31fec6796fef99291b12e833b6bef28100975120a53d209e06d",)),
    "Artistic-1.0": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("b50b4f007fb64c2711e4610c8246d92f9e56b2937c9d25e1acb0416cd5dcc5a8",)),
    "Artistic-1.0-Perl": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("9e18ef4d50faba562e3efa37d8961d6e9956d1c103bfc4fc1caeb07e8effe885",)),
    "Artistic-2.0": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("bae466ac8d7100c304fe76632ccc627203a3897d3f7166f3cce00fb9df8c2074",)),
    "Beerware": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("991d8a58e0b4be84a174a9bd333a8ca33807a0c1ce6d23a2e25a21f7ece482d0",)),
    "Bitstream-Vera": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("5b3b4d3cef4029bda987a71f4bfb6087e40c5e91a2cb853fe7f6ccb78d9d8e38",)),
    "blessing": LicenseText(PUBLIC_DOMAIN, osi=False, fsf=False, digests=("592db1199aab67aafe4515db9808b420db122083807c5329b2f061951e79acbe",)),
    "BlueOak-1.0.0": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("8a1af140fdfbf5afd3df27f7e662f989c5b963a300020dfafce42033cae9e004",)),
    "Boehm-GC": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("9d79ea74534f11db4c04bc9c0175878c0f79641d335ff23d5b5b4d7671924509",)),
    "BSD-1-Clause": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("244e17b6f02493de568a8ddfd7f4349996a6c332c0585d304ceaf5394becea52",)),
    "BSD-2-Clause": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("f32fb3b417a194167cfad068223fc975ba96c5960513a10f66a3c28720aec1df",)),
    "BSD-2-Clause-Patent": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("10500282524f6f9e8c656779f3af9975875ad886181a1f9e7ff0034bcb2f70f5",)),
    "BSD-2-Clause-Views": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("cf2d8e08c5c338804d361fee703f88f52b7fdc710eab87ccd072cf083696f6fc",)),
    "BSD-3-Clause": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("5a93d5831e1297ab10fe643e1a631e83be392896da14ee2951285a79012df69d",)),
    "BSD-3-Clause-Clear": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("4495ed59005765e600a16457dc52a2d39757dbfeae081e68b59fe7c177c78589",)),
    "BSD-3-Clause-LBNL": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("49d63fdb269af8c5209eb1dee7f6d21d7e790785a8774ca8928ddb1ca47ce2c7",)),
    "BSD-3-Clause-Open-MPI": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("a4bd3317cea75d614ac192803a60d6f54dd15a28ea509576d4932778e2879a98",)),
    "BSD-4-Clause": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("c80d927f03db7212ba43a869a383e249c19b40859ff0d9e2e2f968e4fdcf005f",)),
    "BSD-4-Clause-UC": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("c82a5eb55679a3fd483d992c2428e66ee4b9b05b5b78b4ac916692368955b526",)),
    "BSD-Attribution-HPND-disclaimer": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("50dfd9d2ba0a6ec94be6abbf2dc52a0f175ccb8d9bb50df3dde4cc63d783ce3b",)),
    "BSD-Source-Code": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("1f1b677d26ab73e159a55c3a914cf12d888e18a9d0d469fedab4aa6e5a48b1c0",)),
    "BSL-1.0": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("84c6ef3ea9e3254a54d0acf5d3e0c61ae011b8fef7dd6940591cf060e6380a8f",)),
    "BUSL-1.1": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("d90560217049db1d6020ec7cf482da4198bfa372dffd22270ba08fd8088cd93b",)),
    "bzip2-1.0.6": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("0a56dbabe7d2ff65dea26e8b795fe42c6204a192b974d8e9304fe356ccda9fd1",)),
    "C-UDA-1.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("010e48b7389abc09621b16c332d3e7d138ce0815bff07e67a5cb05ef5407058f",)),
    "CC-BY-2.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("f892996eb4b6f2e54241432ed67b743772224e191ba3a0ae595eb4feca3df4c5",)),
    "CC-BY-2.5": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("82d2eddedf451177a12c9753e518352f7f90fd4ca3542111c499be981e9a9600",)),
    "CC-BY-3.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("e6bc9e9c474700b708f568bac9e5a8a9bcb2b1dad53442f5ba449fcb848b8e76", "d748955f9d8e7551cf0a0f23e4bd2d273092e2f942feb3f6fab800e5f6d20300",)),
    "CC-BY-4.0": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("d557539df68e771cc1eedcc91d13f70fca930e508d11eedcafa4b15db49e3744",)),
    "CC-BY-NC-2.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("6093e945ebf2222650e6ac25f0ca304dbd01e3bb72be0699bc1dfdbccb81736c",)),
    "CC-BY-NC-3.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("45526cb7a15234d8d686a3ba5f11b607516f90662d125d5423eb2b57f474e5e4", "7416e6f8832c0185be1ea67d6f36ebb2a9361db297e2ec65e4531aa6cb5b531d",)),
    "CC-BY-NC-4.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("cc97a2c4ff7723a1f25179dc7c18f676fa84191bbb36ea7d0061acaf3c102559",)),
    "CC-BY-NC-ND-3.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("d74e67958cad1ae4cb585402ef66ae41958ba546e6c5e80a93920013cb81bc2a", "36f1672460e4a9dd722f08adebb4254318be954e877a93955dc0f26c4740cf4a",)),
    "CC-BY-NC-ND-4.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("4f3c4a321eb0c73182eee7afa3fe0111e24dec193f2d6febcf4207f26af26839",)),
    "CC-BY-NC-SA-3.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("c6648565ca4a7b22b2cd06e739dfec031313d2f5ce5f31c5c994f6aec9f3fe5e", "2f3a72accf837b99c4c5f277b39098ad8092c107b4d050f948238a54e0a691eb",)),
    "CC-BY-NC-SA-4.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("600ca4e25fe11762b75a97e714707fab48bb778374e92d24c6ca068791661c11",)),
    "CC-BY-ND-3.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("b9d9816260e23b4bdb71e0edc453497273f4cfc1fdf61207ee6cc1c7075a2479", "487b6003909ff94ed0efd31a48fce4b8362c53a96d2eeeb6cc08cce8fb730283",)),
    "CC-BY-ND-4.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("4a9d465e981ae6bde7f117abbfb0e24f2cc1820e99a47efbe58593fa5c43fa29",)),
    "CC-BY-SA-2.0": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("6d6b3bb586e5f4ae7e6a955556442fbfcc2a6cedb89be5e4e0492b2983e40760",)),
    "CC-BY-SA-3.0": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("3f941b3b89cf7b8370ceb83cc76d2120d471b58735d8ca60238a751a48d7f72f", "6e0809a0e41238e84434367205818cd8f48869ba2e6b973f3db43efac7a79735",)),
    "CC-BY-SA-4.0": LicenseText(COPYLEFT, osi=False, fsf=True, digests=("cde7883b9050a1104f4ac19a1572aafd6e5d7323b68351aaf51fbf4beba54966",)),
    "CC-PDDC": LicenseText(PUBLIC_DOMAIN, osi=False, fsf=False, digests=("40f05ccacf018e7311690cd03bba659a1329b3df9775d2aafd33715d52541fe0",)),
    "CC0-1.0": LicenseText(PUBLIC_DOMAIN, osi=False, fsf=True, digests=("a2010f343487d3f7618affe54f789f5487602331c0a8d03f49e9a7c547cf0499",)),
    "CDDL-1.0": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("fde9ef48461569987d09cac803379245dfda04e7751075f39b578a0ce927d470",)),
    "CDDL-1.1": LicenseText(WEAK_COPYLEFT, osi=True, fsf=False, digests=("b5bbb5ac38b5cb76862a7f410ca567e4b32d7c22f22622ebff10ba434827bb78",)),
    "CDLA-Permissive-1.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("52aa39b7c72a3712b1e1ab0b2d6b9d790f443f985407672b9df8edab9eb64712",)),
    "CDLA-Permissive-2.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("4531a67d443284d93ffed0803df5b10634aff21c3d77e381f2d48af01d875868",)),
    "CDLA-Sharing-1.0": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("6419d7a7245cb8a9da6e3dd925d05d824ea0268e3527095f68dec091f84f75da",)),
    "CECILL-2.1": LicenseText(COPYLEFT, osi=True, fsf=False, digests=("4ea234937bc7b0aa5247e436690d1eb9324875bc7590ecde50befd38e35190a5",)),
    "CECILL-B": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("ae13622d13fd432d2e31ed4cb205667d98e203b0cb54b1f244bd8d8354cefd48",)),
    "CECILL-C": LicenseText(WEAK_COPYLEFT, osi=False, fsf=True, digests=("7b00493e6580710aad04faf2747d74b6de840a47183f76f5a3645f1fce69245d",)),
    "CFITSIO": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("805453d73d82fa5bedec1ab5afdeaaecd3797e6ad735f5b58324ce0363a941a0",)),
    "ClArtistic": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("70fbadfe7d99a766987cba5748021824ce261ea6fa6147a1ae6a451f100cb90e",)),
    "CMU-Mach": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("22eeb34e3e2a837f21ad0de0fcb13af7a4fa4c2d61a999c34d4a8ad62d3ca98d",)),
    "CNRI-Python": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("69b5d7d41a6ece3b20c6a77774f567948766d8643f7646e3f65474692cce5c15",)),
    "copyleft-next-0.3.1": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("ef241a58203ce1474141d3b014ff59d5a22b31248263e8dd78f7b5ff367e97bb",)),
    "CPL-1.0": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("9698f2ba346a875a47a9cc6bb602e3d126758774dde1f575b4f3369dc3f2574f",)),
    "curl": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("272d1239bdcef73bb98bac206168ca1040bd4a31296755fadfb7515bcea18df3",)),
    "DL-DE-BY-2.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("6c0be9ac9d97f3ef693a1758bb301daa1717ee8ee7854110c079162e9edcbe2a",)),
    "dtoa": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("8222bef21419a8734583669c5d94ebe557f8f6309c8c53fcc0bb49953bbc790b",)),
    "ECL-2.0": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("49831d277a9eac4e7a31922554e24fb61142e7a6534da2d15fb062ce0dca77bf",)),
    "Elastic-2.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("6448026f2e06ab3da9ed8ddda686694bba37179e88db92dea6d4aab516c957ea", "48255018b41fc0e965b1115af7e6779bc218bb8a6747d561da800d5022622aa2",)),
    "EPL-1.0": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("f3d8dc494be2e7ea9cd31ac2fb902e35dce7ff8181b9b30d05861b47c339cb25",)),
    "EPL-2.0": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("61616a4066701bc7888ad910de1633dbc55eb4d4d30d930feef55afb9492e33c",)),
    "etalab-2.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("f82596bde5c4abc62b4e63b1a3ba555f6b9203a340765fb178d188958600d111",)),
    "EUPL-1.1": LicenseText(COPYLEFT, osi=True, fsf=True, digests=("1480d19681cfc637b0e83b9b52fe9fc91bdf571d83e7d4ee141c2860b1b15b37",)),
    "EUPL-1.2": LicenseText(COPYLEFT, osi=True, fsf=True, digests=("57fb42fbcd0b037ce528ed8f72f1ec095d67bc6825ecf1448ff39be1fe68a4b4",)),
    "FDK-AAC": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("c90edceefc6bd9352cc44912a94fcf74a683d918a3995deadefbf10a6770f6c2",)),
    "FSFAP": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("99cb91dacd5714a701749ec47239015726beec5fc86a5c94a4873778824b16d7",)),
    "FSFUL": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("a5e99f55b10212abd954f092b65ef5d4cf4047071fe20be307406a148a342c35",)),
    "FSFULLR": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("6d425c2caac889c969c8c2ebb18b1e2326f0d8c7f82e43c0e6c9c957fe3c6cd0",)),
    "FSFULLRWD": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("e1d905d45b956dcf22c5a7bebd382f3c8c90256ee46148dd479fc821097522d2",)),
    "FSL-1.1-ALv2": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("36b6082235c0a2105174927fc57cc6ae9c41f45a08af2bdcaee18a8dace56177",)),
    "FSL-1.1-MIT": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("0905af4c021cb64aff11a80aae7f25f0d7bafc69c14e97992273873d175a718b",)),
    "FTL": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("ced6622122ce451cb1ea0c3c3f507a640e2a44c075c04900ddd9fae8acb5369f",)),
    "GFDL-1.1-invariants-only": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("25fa19cb49d292047f3f16879301ae3a496b263a938f74e1840906d97f6d0211",)),
    "GFDL-1.1-invariants-or-later": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("25fa19cb49d292047f3f16879301ae3a496b263a938f74e1840906d97f6d0211",)),
    "GFDL-1.1-no-invariants-only": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("25fa19cb49d292047f3f16879301ae3a496b263a938f74e1840906d97f6d0211",)),
    "GFDL-1.1-no-invariants-or-later": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("25fa19cb49d292047f3f16879301ae3a496b263a938f74e1840906d97f6d0211",)),
    "GFDL-1.1-only": LicenseText(COPYLEFT, osi=False, fsf=True, digests=("25fa19cb49d292047f3f16879301ae3a496b263a938f74e1840906d97f6d0211",)),
    "GFDL-1.1-or-later": LicenseText(COPYLEFT, osi=False, fsf=True, digests=("25fa19cb49d292047f3f16879301ae3a496b263a938f74e1840906d97f6d0211",)),
    "GFDL-1.2-invariants-only": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("1118a91a3290bc8981f922dfcffc1a619592c007aa31b7a98cee694d933c86ce",)),
    "GFDL-1.2-invariants-or-later": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("1118a91a3290bc8981f922dfcffc1a619592c007aa31b7a98cee694d933c86ce",)),
    "GFDL-1.2-no-invariants-only": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("1118a91a3290bc8981f922dfcffc1a619592c007aa31b7a98cee694d933c86ce",)),
    "GFDL-1.2-no-invariants-or-later": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("1118a91a3290bc8981f922dfcffc1a619592c007aa31b7a98cee694d933c86ce",)),
    "GFDL-1.2-only": LicenseText(COPYLEFT, osi=False, fsf=True, digests=("1118a91a3290bc8981f922dfcffc1a619592c007aa31b7a98cee694d933c86ce",)),
    "GFDL-1.2-or-later": LicenseText(COPYLEFT, osi=False, fsf=True, digests=("1118a91a3290bc8981f922dfcffc1a619592c007aa31b7a98cee694d933c86ce",)),
    "GFDL-1.3-invariants-only": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("d024962f1f4966c102b40d9fd9cc834a3f0b3c1397b41444b7cd7925640dc929",)),
    "GFDL-1.3-invariants-or-later": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("d024962f1f4966c102b40d9fd9cc834a3f0b3c1397b41444b7cd7925640dc929",)),
    "GFDL-1.3-no-invariants-only": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("d024962f1f4966c102b40d9fd9cc834a3f0b3c1397b41444b7cd7925640dc929",)),
    "GFDL-1.3-no-invariants-or-later": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("d024962f1f4966c102b40d9fd9cc834a3f0b3c1397b41444b7cd7925640dc929",)),
    "GFDL-1.3-only": LicenseText(COPYLEFT, osi=False, fsf=True, digests=("d024962f1f4966c102b40d9fd9cc834a3f0b3c1397b41444b7cd7925640dc929",)),
    "GFDL-1.3-or-later": LicenseText(COPYLEFT, osi=False, fsf=True, digests=("d024962f1f4966c102b40d9fd9cc834a3f0b3c1397b41444b7cd7925640dc929",)),
    "GPL-1.0-only": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("da37ffd5fcdf0a7d76af862a4330ea1076e1443b51782b2f912148357b7acabe",)),
    "GPL-1.0-or-later": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("da37ffd5fcdf0a7d76af862a4330ea1076e1443b51782b2f912148357b7acabe",)),
    "GPL-2.0-only": LicenseText(COPYLEFT, osi=True, fsf=True, digests=("aaf135472f81c5b4a0dca9367e5bb5e9750032b5bebe5442b36e4c0a47430df3",)),
    "GPL-2.0-or-later": LicenseText(COPYLEFT, osi=True, fsf=True, digests=("aaf135472f81c5b4a0dca9367e5bb5e9750032b5bebe5442b36e4c0a47430df3",)),
    "GPL-3.0-only": LicenseText(COPYLEFT, osi=True, fsf=True, digests=("fb981668c18a279e285fc4d83fba1e836cc84dd4daa73c9697d3cfd2d8aca6e0", "b6a8bf63ae2c95b0aa256ee774680d7bd18f21f457da75466f7610b3b16acc4e",)),
    "GPL-3.0-or-later": LicenseText(COPYLEFT, osi=True, fsf=True, digests=("fb981668c18a279e285fc4d83fba1e836cc84dd4daa73c9697d3cfd2d8aca6e0", "b6a8bf63ae2c95b0aa256ee774680d7bd18f21f457da75466f7610b3b16acc4e",)),
    "HaskellReport": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("5d28692ce2562cd7e0eb0ed6f087f84e71663b14d319e0ebb2abc8f84f1e308f",)),
    "Hippocratic-2.1": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("e64f3c8d11dcff7bcbe739ae7b2d3ef755cd3dadf47273ad3cdebbda8a40f58a",)),
    "HP-1986": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("9aa6d2493f6a371ce19d3538f352bbd80ee81190c4403cda7f1d9a6d878cb41c",)),
    "HPND": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("4d9ae4a36816338dfa153d1d6f92645c45f25eaa46908d3096922e2431fb571e", "840be799ac5a196cad1f0c8c8ca7970c48ec177d7091162bd8e2ad16caf694b2",)),
    "HPND-Intel": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("9ef7a1ef2c62df2512378aa071bf5d2a5f7842b8230585401ceb3e1d99628701",)),
    "HPND-Markus-Kuhn": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("d81e0150606531af1ea188093a1e62846a51c83eaeebaaa1cc7e94a3415deced",)),
    "HPND-merchantability-variant": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("6fbed4fe5e3ce1a51aa3fe597f16dd9619c36ae4f1e4cf850dd97cf1fc0bb5d9",)),
    "HPND-sell-variant": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("235abc578371bf9861e3d6eee0a9ad16228f9be18408e17fc569c6a98ba69d5f",)),
    "ICU": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("c7922dccc709c9e19ab6ba970fe00af174229484b05160fb07a3913f4d04cf50",)),
    "IJG": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("7658542977bfdced9e1059a6c934ce4281de76d103b831007b825917dc62511a",)),
    "Info-ZIP": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("fa0c6949eee13c4099a6d0f78f9f1d76c7320e71adfc066d1632837320703a08",)),
    "Inner-Net-2.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("1beeb7b196011ecb20843a6de48dd34346341a3c6c97923ac2cb5846f4320d27",)),
    "IPL-1.0": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("0d7fbdd478bb103ab440618af177275cbc47d936b1703214091a360dd36116e0",)),
    "ISC": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("521c6f0ed8e64736684f89843d16a4df6b4b7449acbcfc6ca6630a3037ca8c53", "f2ec607f67bb0dd3053b49835b02110d5cd0f8eb6da3aac4dc0b142a6b299be9",)),
    "Knuth-CTAN": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("3b61d86ae3fd94caa0febd5b24f68cf37f8dd915e142083bb966c4ca037634ce",)),
    "LGPL-2.0-only": LicenseText(WEAK_COPYLEFT, osi=True, fsf=False, digests=("86dc99d7e5060915ab1dfc1378b7dd351c62088bfa74067e8aa1868c6fdba7d8", "61778e80a2fd85955b626b29aa2bcf06144c714277bded65633e4a81479d9fb3",)),
    "LGPL-2.0-or-later": LicenseText(WEAK_COPYLEFT, osi=True, fsf=False, digests=("86dc99d7e5060915ab1dfc1378b7dd351c62088bfa74067e8aa1868c6fdba7d8", "61778e80a2fd85955b626b29aa2bcf06144c714277bded65633e4a81479d9fb3",)),
    "LGPL-2.1-only": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("5749785c8bdefafcb5d798270ed0a967036fe2ca63dcedade1627565dfef81d2", "fa6f36630bb1e0c571d34b2bbdf188d08495c9dbf58f28cac112f303fc1f58fb",)),
    "LGPL-2.1-or-later": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("5749785c8bdefafcb5d798270ed0a967036fe2ca63dcedade1627565dfef81d2", "fa6f36630bb1e0c571d34b2bbdf188d08495c9dbf58f28cac112f303fc1f58fb",)),
    "LGPL-3.0-only": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("996af0513df21f7496288951c41428a03c174e9e4a9d63665c57d670f845ccb1",)),
    "LGPL-3.0-or-later": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("996af0513df21f7496288951c41428a03c174e9e4a9d63665c57d670f845ccb1",)),
    "Libpng": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("7667a8c88c7a63690244988d626bcddd27ed895526e2c3ab1a9adb463a5fa287",)),
    "libpng-2.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("f207d0f7375b977f76ce2437962c1c12a646f15cda3df48d5472ac95eb68e8ff", "8232f6aa4a06606e1abea065d1a34d4eed1b1cdc0c0ffdcb40980cf3c1716acd",)),
    "libtiff": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("a6ecaa20c8c1b7a8215ed05e5f58764f821596e36d31acc05282c6154cf0dc44",)),
    "Linux-man-pages-copyleft": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("e7241c6405f8ad70943bcdb0fd83cb79fb25eec601ecf311acc9c72f19794d5a",)),
    "Linux-man-pages-copyleft-2-para": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("9404a643cfa5e118b50ac14584f5ed01d63f2e14d7249068f3b69bdda73846ec",)),
    "Linux-OpenIB": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("6503d0418051ea29997c4c4d950386fcc356c9459a73a62d5d67707d04739b92",)),
    "LPPL-1.3a": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("d1d977d5a7f0dd55e800a1827f6eb0241da6e0c32cbb7d66b8feae98834bf003",)),
    "LPPL-1.3c": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("f0f307427080e67974f58f6b7f732039a966b3a7ae480dedee3275907104ac1f",)),
    "LZMA-SDK-9.22": LicenseText(PUBLIC_DOMAIN, osi=False, fsf=False, digests=("6e31b1f0c4ae78e10710525955b2356adec878c918e86d8ee796b2925223ab88",)),
    "Martin-Birgmeier": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("6162a9f6c4a186b562bf090cd3f57c5f1f681480eecbde75beeaffaca5bd7040",)),
    "MirOS": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("79da9e5c37adc25a27cfa41f1f64cc93cf2d6be8e52cba3fbaf02b9e8fafd3d9",)),
    "MIT": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("b05785f9f18e6716bab63424b11454513b9943a222595b70411009202fc592b5", "6d7c9e9d7d12c8d3753bdb24417b84819ba4d6c2f025c37948113d86dc81eef9", "b85dcd3e453d05982552c52b5fc9e0bdd6d23c6f8e844b984a88af32570b0cc0",)),
    "MIT-0": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("59746d6285ffa44bfc7ecada352aa5d6a20dc8eab418a60ce091cc739012c135",)),
    "MIT-CMU": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("9454a04f14e2a889b3f08bd9973f39889378cb5bbb64008533c36ea685cda936",)),
    "MIT-Festival": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("8ae0bc761d5f10ff26a9b13d26e8f81c2eaed5081cf0c62582df3a5aad15fa2f",)),
    "MIT-Khronos-old": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("fbeaca472f4f70e276dd1106ca5097435967a22ad6c1d8200aef7ad9f70aaf3f",)),
    "MIT-Modern-Variant": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("d7366190045ad81a5e612799b8f4eef242f9b389a0040836aad61bebe9849647",)),
    "MIT-open-group": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("d5942d51b7e079b0eab4d6f9e06f1b8625f918b8802117a9c3127c004c5df9b6", "8e06c2e84fe9275766d1c22a5f194cd264f9664c568c3bdf1356624a65a4cfc4",)),
    "MIT-Wu": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("c0cb8b3ce560f5e3dc4d55e65e3da650e76ae45863066fa5f5d5a1439db5e153",)),
    "MPL-1.1": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("6214f8b1300bb9f37b16ebc146f8f61af0187e0025042ff95c8b3030744a8795",)),
    "MPL-2.0": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("66a3107d5ad6a058aab753eaac2047ccb2ed0e39465dd0fe5844da3e300d5172",)),
    "MPL-2.0-no-copyleft-exception": LicenseText(WEAK_COPYLEFT, osi=True, fsf=False, digests=("66a3107d5ad6a058aab753eaac2047ccb2ed0e39465dd0fe5844da3e300d5172",)),
    "MS-PL": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("7a162b1da10f1c22db4c68f07bec4a8355259f8d4aab5b00a2b2bbd423d833dd",)),
    "MS-RL": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("7c69e20ff8633d1b5135126aa9921a2156f6a52b355e92812a084211afdd5d38",)),
    "MulanPSL-2.0": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("eb7a1d713eb919b146787629e22e4c975cb701f529a65d4d7e0fcd417558bf1c",)),
    "NAIST-2003": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("0cc49a2743d845a9f3ea44ec3d7a6acc0aa8f0380772020310f3389713c45cbc",)),
    "NCSA": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("b31bbfd06e221b39e1a877fb3f91b32fe9e88c87c1b46d11cd2bd5c822ec71e3",)),
    "NTP": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("290eb479deee5ab45484cb98f7cd6c118603208a4f4a118e34f591b50fa20934",)),
    "O-UDA-1.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("7bf99bb9cff7b91883ff56e1eabbf673dd5d9ce163316735d42a645a5cb1f1b2",)),
    "OAR": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("07cffb7517187e810518a3dcb1c4aac6c99f061ba87888539cb20ae5821b21e4",)),
    "ODbL-1.0": LicenseText(COPYLEFT, osi=False, fsf=True, digests=("77d2692c3d64efdd4db18dce2407699baf62930e2b50d6e5ed90b48acf16b7c1",)),
    "ODC-By-1.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("b1c4de4bf91c95eec945ea7cbe0575fd52e7ee8fa430acb01d323d8214b1c482",)),
    "OFL-1.0": LicenseText(WEAK_COPYLEFT, osi=False, fsf=True, digests=("7e6a2968a3782383e1b427aafaf7f7c7737fff024294c4a4d0347557b1aecb10",)),
    "OFL-1.1": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("8eea8287e5876b539670cadb82e99f9a7afddec6f6730811be1daf25d2e9bcfd",)),
    "OFL-1.1-no-RFN": LicenseText(WEAK_COPYLEFT, osi=True, fsf=False, digests=("8eea8287e5876b539670cadb82e99f9a7afddec6f6730811be1daf25d2e9bcfd",)),
    "OFL-1.1-RFN": LicenseText(WEAK_COPYLEFT, osi=True, fsf=False, digests=("8eea8287e5876b539670cadb82e99f9a7afddec6f6730811be1daf25d2e9bcfd",)),
    "OGL-UK-2.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("dea956be58d1944c806229baeb5f4c3ade45decc2c855a1d738ae0ad014ff408",)),
    "OGL-UK-3.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("eb22384c866b63b513254ae3baff6b3bc7d001f559d03e02b6b8b83333a04806",)),
    "OLDAP-2.8": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("bf30f8254b6698a79e867d0a7d641bbbd3460f7c2f16cab9c6816718815c2ded",)),
    "OpenSSL": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("57bf9f4cefe46ae1ef5b3ae83505f01ec8b2ac94a473e2c9e3fb1f80de1e23b8",)),
    "OSL-3.0": LicenseText(COPYLEFT, osi=True, fsf=True, digests=("9329b26bc40104b49da35be76b7e972dada9f2561ca19d17aed4801bf5e1623c",)),
    "Parity-7.0.0": LicenseText(COPYLEFT, osi=False, fsf=False, digests=("5b3bf2359dfb9f95483270da7833be8fcc9e1758abffabb045991ec7c51ef796",)),
    "PDDL-1.0": LicenseText(PUBLIC_DOMAIN, osi=False, fsf=False, digests=("561ca6d9bba2a5b8658466833b15035f64d067fc327d226250f5123aa96e82af",)),
    "PHP-3.0": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("8c18adf2c052e52aa8376d40a445a840af2edab91aa5892a78bddf9421cf0a9d",)),
    "PHP-3.01": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("c288e228ae7ccd27a39a7d4011057c85769a85e5fa38e91e31cec1f5549b1e7e",)),
    "PolyForm-Noncommercial-1.0.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("ffcca38841adb694b6f380647e15f17c446a4d1656fed51a1e2041d064c94cc8",)),
    "PolyForm-Small-Business-1.0.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("2f81e317d36f83a199c9fa10bcb250775fd90510303eaeb15aac2d8f0c1fc2d6",)),
    "PostgreSQL": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("d443a7dc550da2be365b4e924bb8779101e1c318d15bb78e5cb6374cadd8b6c0",)),
    "PSF-2.0": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("ab745c5061d1dea43a3885e5b4b6befc7e983954954775c5736debeefcdfd89b",)),
    "Python-2.0": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("893c2bafbb8133f7aa97e1f79a3ee3241ebca7025f56278e9e1f72bb98592f9d",)),
    "Python-2.0.1": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("1d165c0d255094285fe6ce754b431b9efc1e7df547db4aed5c3b3d082e5d5aaa",)),
    "QPL-1.0": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("524dce795dfcfda14f8ee06e738f3059c4a054306d07dc14601164ed94cd01ce",)),
    "Ruby": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("dac64527dc40e0be96d3316a93c0b5adb64ae47c7dc546fbc9fae4f0268cf6b6",)),
    "Sendmail": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("3e9d88d6efc1150456f660c2b375be7e9af5d7147bf90bb6c79f4b25a1e21424",)),
    "SGI-B-2.0": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("cbe043cc819b6afbac13c11d83c8bdcd566d1539881f17cab28615edd7af3acf",)),
    "SISSL": LicenseText(WEAK_COPYLEFT, osi=True, fsf=True, digests=("51a1c03478872c36943737b04cc35a6f903d4f3a6bd9144c90af5831f44c0e75", "b78e4ae11663b1c3776e68df59128c2e4fbcd8a243c3a21c36f5cc48e55d7d13",)),
    "Sleepycat": LicenseText(COPYLEFT, osi=True, fsf=True, digests=("74e179cd51dffda479cf3a339cf53c4dc46289fd72307f246af6c45de7b9cb5c",)),
    "SMLNJ": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("772a7c6e457671d358559c6f242da9dc79f63b9301c93e22314cd5c032a1991e",)),
    "Spencer-94": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("7fc18e76f0c124a3a568657b99e33df9bda7e4f71a493dd63d69956eb2117660",)),
    "SSPL-1.0": LicenseText(LIMITED_USE, osi=False, fsf=False, digests=("3fac2f3a7404f72330ae38e3a1d2632ede9ad3fbdb0d471d04c33f2c1d0e94ca",)),
    "SunPro": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("f5ab1c2763182c84a0b991a82d57b4176df0d109ca5d485d8446585696b26894",)),
    "TCL": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("36fc2d8a56a2a014ded51e298a7e63635eb4ec62e8bedb99c82b892542ac5e7d",)),
    "Ubuntu-font-1.0": LicenseText(WEAK_COPYLEFT, osi=False, fsf=False, digests=("2f0015108d68627bd788d313f529c21ff4da2c2c42a5e1f3883acc83480f9002",)),
    "Unicode-3.0": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("f7db81051789b729fea528a63ec4c938fdcb93d9d61d97dc8cc2e9df6d47f2a1",)),
    "Unicode-DFS-2015": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("89e104c664787a7e98d6ccb8c5c24fe8033ed75421ccbcfd360222732b5a3ccd",)),
    "Unicode-DFS-2016": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("1a33fc12a3abb0b3eb14db70e36ef1d0af51c93b1cc1f80f022c3bd406e9b874",)),
    "Unlicense": LicenseText(PUBLIC_DOMAIN, osi=True, fsf=True, digests=("0bdebfeda07d45dada625ae1317c6f833186e798b171d0db640bcf32e92a8240",)),
    "UPL-1.0": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("2f81e75033a0c872ab9725b36081e363f0c9e6c45585c35930edb66043fdc576",)),
    "Vim": LicenseText(COPYLEFT, osi=False, fsf=True, digests=("2812610ab6a7304edf0282e236f9dd2bd9e47db05f3abed7538c57173d1ed73b",)),
    "W3C": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("df7429635bacfb82b3e92c2fff50553949afd97389890d53a67b67cbf9fba68e",)),
    "W3C-19980720": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("8b3984236d53eda13af7f1b01078b33e4390608523865aa1ddbc4083e6492c95", "038839fbeeedc3e12d626b4331b6f5ff01d64dc52e595f44192dee93ae94c20c",)),
    "W3C-20150513": LicenseText(PERMISSIVE, osi=True, fsf=False, digests=("38b688e653a972897f6a875c53abbf867b261cbeba7ded0bdf100ae9907a6c19",)),
    "WTFPL": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("13d8825a59e5e546d41e8711405d0f335a9934362a47c04a3b4280fa7932dc4a",)),
    "X11": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("e1963c9958b93bc2578b34f8ae22cfe205e8c0e7d5be7235cfdbdefbf92a5423",)),
    "X11-distribute-modifications-variant": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("472a5d3bfe4ba4289fbe4d3ff016be25ad4af4c6d7ea523d443139b56d214130",)),
    "Zend-2.0": LicenseText(PERMISSIVE, osi=False, fsf=True, digests=("c501915ee7c588173de03db9a8ca105c1baadb16bb05bca211700b850bcb077d",)),
    "Zlib": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("bfb1112d49db5b1daecdfef24bd7e2f3ea0bafb33aa67aa0ab51e2bf8407c03d",)),
    "zlib-acknowledgement": LicenseText(PERMISSIVE, osi=False, fsf=False, digests=("aeb83cf7f4c9076691f72856d3b8f64a152027e9b1a136d399198c683fd26c12",)),
    "ZPL-2.0": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("f7a42094e22b669aaa352720e6dc93d49da430d8cb25ebf904b50faa3fd98b20",)),
    "ZPL-2.1": LicenseText(PERMISSIVE, osi=True, fsf=True, digests=("3ab20c1a75502ea2231423bf6097a2cf56e336236c1153c2b9a4cb01608fb829",)),
}


def license_class(identifier: str) -> Optional[str]:
    """The table's class of `identifier`, or None for one the table does not hold."""
    entry = LICENSE_TEXTS.get(identifier)
    return entry.license_class if entry else None


def describe_classes(identifiers: Iterable[str]) -> str:
    """`MIT (permissive), X (unclassed)`, for a summary line."""
    return ", ".join(f"{identifier} ({license_class(identifier) or 'unclassed'})" for identifier in sorted(identifiers))


def read_declared_digests(publisher: Optional[KeyValueDocument]) -> Tuple[Dict[str, Set[str]], Optional[str]]:
    """The `license_text_digests` pairs of `publisher.conf` by identifier, and the reason the list is refused where
    it is (the pairs are then empty, so an unpinned text warns and a pinned one is held to the table alone)."""
    if publisher is None or not publisher.has(DECLARED_DIGESTS_KEY):
        return {}, None
    items, reason = publisher.get_list(DECLARED_DIGESTS_KEY)
    if reason is not None:
        return {}, f"`{DECLARED_DIGESTS_KEY}` is not a list ({reason})"
    declared: Dict[str, Set[str]] = {}
    for item in items:
        match = DECLARED_DIGEST_ITEM.match(item)
        if match is None:
            return {}, f"`{DECLARED_DIGESTS_KEY}` item `{item}` is not `<identifier>:<sha256>`, the digest 64 lower-case hex digits"
        declared.setdefault(match.group("identifier"), set()).add(match.group("digest"))
    return declared, None


def check_license_text(collector: FindingCollector, path: str, identifier: str, digest: Optional[str],
                       declared: Dict[str, Set[str]]) -> None:
    """Hold the text of `identifier` read at `path`, by its SHA-256 `digest` (None for a text read in part), to the
    published digests and the declared ones."""
    entry = LICENSE_TEXTS.get(identifier)
    accepted = set(entry.digests if entry else ()) | declared.get(identifier, set())
    if not accepted:
        collector.warn(path, "license.text-unpinned",
                       f"these tools do not hold the published digest of `{identifier}`, so the text is not compared; "
                       f"declare the digest of `reuse download {identifier}`'s text in publisher.conf `{DECLARED_DIGESTS_KEY}` to check it")
    elif digest not in accepted:
        collector.warn(path, "license.text-modified",
                       f"is not the `{identifier}` text as published (sha256 {digest or 'of a partial read'}); write it "
                       f"with `reuse download {identifier}`, which keeps the holder line a placeholder, and state the "
                       "holder in REUSE.toml or a file header")


def check_license_text_directory(collector: FindingCollector, root_fd: int, declared: Dict[str, Set[str]]) -> None:
    """Hold every `LICENSES/<identifier>.txt` under `root_fd` to the table, each read through `safe_read`; a text
    the reader refuses is refused under `license.text`, and an absent directory is the caller's to report."""
    try:
        directory_fd = open_directory(fmt.LICENSE_TEXTS_DIRECTORY, root_fd)
    except FileNotFoundError:
        return
    except (RefusedRead, NotADirectoryError):
        collector.refuse(fmt.LICENSE_TEXTS_DIRECTORY, "license.text", "is not a directory; the licence texts are files in one")
        return
    except OSError as error:
        collector.refuse(fmt.LICENSE_TEXTS_DIRECTORY, "license.text", f"cannot be read ({error.strerror}); a text this check has not read is not taken as published")
        return
    try:
        try:
            with os.scandir(directory_fd) as listing:
                names = sorted(entry.name for entry in listing if entry.name.endswith(".txt"))
        except OSError as error:
            collector.refuse(fmt.LICENSE_TEXTS_DIRECTORY, "license.text", f"cannot be read ({error.strerror}); a text this check has not read is not taken as published")
            return
        for name in names:
            path = f"{fmt.LICENSE_TEXTS_DIRECTORY}/{name}"
            try:
                read = read_file(name, directory_fd, fmt.FILE_MAX_BYTES)
            except RefusedRead as refusal:
                collector.refuse(path, "license.text", refusal.message)
                continue
            except OSError as error:
                collector.refuse(path, "license.text", f"cannot be read ({error.strerror}); a text this check has not read is not taken as published")
                continue
            check_license_text(collector, path, name[:-len(".txt")], read.digest, declared)
    finally:
        os.close(directory_fd)
