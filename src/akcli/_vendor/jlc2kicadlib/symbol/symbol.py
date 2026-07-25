import json
import logging
import os
import re

from .. import _http as requests

from .. import helper
from .symbol_handlers import handlers

template_lib_header = """\
(kicad_symbol_lib (version 20210201) (generator TousstNicolas/JLC2KiCad_lib)
"""

template_lib_footer = ")\n"

supported_value_types = [
    "Resistance",
    "Capacitance",
    "Inductance",
    "Frequency",
]  # define which attribute/value from JLCPCB/LCSC will be added in the "value" field

# --- akcli deviations (see ../PROVENANCE.md) --------------------------------- #
# Reference prefixes of two-pin passive parts. EasyEDA gives their pins a
# meaningless electric code (a plain 0603 resistor ships two "input" pins), so
# the converter passes ``passive`` down to the pin handler as the default.
PASSIVE_PREFIXES = frozenset({"R", "C", "L", "FB", "RN", "VR"})
PASSIVE_TAGS = ("resistor", "capacitor", "inductor", "ferrite")

# Value-carrying EasyEDA attributes, most specific first. ``Value`` is last
# because on many parts it merely repeats the MPN (guarded against below).
VALUE_ATTR_KEYS = ("Resistance", "Capacitance", "Inductance", "Frequency", "Value")

# Unit suffixes dropped when normalizing to the KiCad house style ("10kΩ" ->
# "10k", "100nF" -> "100n"). Frequencies keep their "Hz".
_UNIT_SUFFIXES = ("ohms", "ohm", "Ω", "ω", "F", "H")


def _quote_safe(text):
    """Neutralize characters that would break the quoted s-expression string."""
    return (
        str(text or "")
        .replace("\\", "{backslash}")
        .replace('"', "{dblquote}")
        .replace("\n", " ")
    )


def normalize_value(raw):
    """Normalize an EasyEDA value attribute to KiCad house style, or ``""``."""
    text = str(raw or "").strip().replace("μ", "u").replace("µ", "u")
    text = re.sub(r"\s+", "", text)
    if not text:
        return ""
    if not re.match(r"^[0-9.]", text):
        return text                      # not a magnitude (e.g. "N/A"): as-is
    if not text.lower().endswith("hz"):
        for suffix in _UNIT_SUFFIXES:
            if len(text) > len(suffix) and text.endswith(suffix):
                text = text[: -len(suffix)]
                break
    # "10K" -> "10k" (KiCad writes SI multipliers lower-case except M/G)
    if text[-1:] in ("K", "N", "P", "U", "R"):
        text = text[:-1] + text[-1].lower()
    return text


def component_value(c_para):
    """The part's actual value ("10k", "100n") from EasyEDA attributes, or ``""``."""
    if not isinstance(c_para, dict):
        return ""
    mpn_like = {
        str(c_para.get(k, "")).strip()
        for k in ("name", "Manufacturer Part", "Supplier Part")
    }
    for key in VALUE_ATTR_KEYS:
        raw = str(c_para.get(key, "") or "").strip()
        if not raw or raw in mpn_like:
            continue                     # empty, or just a repeat of the MPN
        normalized = normalize_value(raw)
        if normalized:
            return normalized
    return ""


def default_electrical_type(prefix, tags, pin_count):
    """``"passive"`` for two-pin R/C/L-class parts, else ``""`` (no override)."""
    if pin_count != 2:
        return ""
    if str(prefix or "").upper() in PASSIVE_PREFIXES:
        return "passive"
    blob = " ".join(str(t) for t in (tags or [])).lower()
    if any(word in blob for word in PASSIVE_TAGS):
        return "passive"
    return ""


def create_symbol(
    symbol_component_uuid,
    footprint_name,
    datasheet_link,
    library_name,
    symbol_path,
    output_dir,
    component_id,
    skip_existing,
):
    class kicad_symbol:
        drawing = ""
        pinNamesHide = "(pin_names hide)"
        pinNumbersHide = "(pin_numbers hide)"

    kicad_symbol = kicad_symbol()

    ComponentName = ""
    ComponentValue = ""        # akcli: actual R/C/L value, "" when the part has none
    ComponentMPN = ""          # akcli: manufacturer part number (Value used to hold it)
    for component_uuid in symbol_component_uuid:
        response = requests.get(
            f"https://easyeda.com/api/components/{component_uuid}",
            headers={"User-Agent": helper.get_user_agent()},
        )
        if response.status_code == requests.codes.ok:
            data = json.loads(response.content.decode())
        else:
            logging.error(
                f"create_symbol error. Requests returned with error code "
                f"{response.status_code}"
            )
            return ()

        symbol_shape = data["result"]["dataStr"]["shape"]
        symmbol_prefix = data["result"]["packageDetail"]["dataStr"]["head"]["c_para"][
            "pre"
        ].replace("?", "")
        component_title = (
            data["result"]["title"]
            .replace(" ", "_")
            .replace(".", "_")
            .replace("/", "{slash}")
            .replace("\\", "{backslash}")
            .replace("<", "{lt}")
            .replace(">", "{gt}")
            .replace(":", "{colon}")
            .replace('"', "{dblquote}")
        )

        component_types_values = []
        for value_type in supported_value_types:
            if value_type in data["result"]["dataStr"]["head"]["c_para"]:
                component_types_values.append(
                    (
                        value_type,
                        data["result"]["dataStr"]["head"]["c_para"][value_type],
                    )
                )

        # akcli: value + MPN attributes, and the passive-class pin-type default
        symbol_c_para = data["result"]["dataStr"]["head"].get("c_para") or {}
        if not ComponentValue:
            ComponentValue = component_value(symbol_c_para)
        if not ComponentMPN:
            ComponentMPN = str(
                symbol_c_para.get("Manufacturer Part")
                or symbol_c_para.get("name")
                or ""
            ).strip()
        kicad_symbol.default_electrical_type = default_electrical_type(
            symmbol_prefix,
            data["result"].get("tags"),
            sum(1 for line in symbol_shape if str(line).startswith("P~")),
        )

        if not ComponentName:
            ComponentName = component_title
            component_title += "_0"
        if (
            len(symbol_component_uuid) >= 2
            and component_uuid == symbol_component_uuid[0]
        ):
            continue

        # if library_name is not defined, use component_title as library name
        if not library_name:
            library_name = ComponentName

        filename = f"{output_dir}/{symbol_path}/{library_name}.kicad_sym"

        logging.info(f"Creating symbol {component_title} in {library_name}")

        kicad_symbol.drawing += f'''\n    (symbol "{component_title}_1"'''

        for line in symbol_shape:
            args = [i for i in line.split("~")]  # split arguments
            model = args[0]
            logging.debug(args)
            if model not in handlers:
                logging.warning("symbol : parsing model not in handler : " + model)
            else:
                handlers.get(model)(
                    data=args[1:],
                    translation=(
                        data["result"]["dataStr"]["head"]["x"],
                        data["result"]["dataStr"]["head"]["y"],
                    ),
                    kicad_symbol=kicad_symbol,
                )
        kicad_symbol.drawing += """\n    )"""

    # ruff: disable [E501]
    template_lib_component = f"""\
  (symbol "{ComponentName}" {kicad_symbol.pinNamesHide} {kicad_symbol.pinNumbersHide} (in_bom yes) (on_board yes)
    (property "Reference" "{symmbol_prefix}" (id 0) (at 0 1.27 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "{_quote_safe(ComponentValue) or ComponentName}" (id 1) (at 0 -2.54 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" "{footprint_name}" (id 2) (at 0 -10.16 0)
      (effects (font (size 1.27 1.27) italic) hide)
    )
    (property "Datasheet" "{datasheet_link}" (id 3) (at -2.286 0.127 0)
      (effects (font (size 1.27 1.27)) (justify left) hide)
    )
    (property "ki_keywords" "{component_id}" (id 4) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "LCSC" "{component_id}" (id 5) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    (property "MPN" "{_quote_safe(ComponentMPN) or ComponentName}" (id 6) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )
    {get_type_values_properties(7, component_types_values)}{kicad_symbol.drawing}
  )
"""
    # ruff: enable [E501]

    if not os.path.exists(f"{output_dir}/{symbol_path}"):
        os.makedirs(f"{output_dir}/{symbol_path}")

    if os.path.exists(filename):
        update_library(
            library_name,
            symbol_path,
            ComponentName,
            template_lib_component,
            output_dir,
            skip_existing,
        )
    else:
        with open(filename, "w", encoding="utf-8", newline="\n") as f:
            logging.info(f"writing in {filename} file")
            f.write(template_lib_header)
            f.write(template_lib_footer)
        update_library(
            library_name,
            symbol_path,
            ComponentName,
            template_lib_component,
            output_dir,
            skip_existing,
        )


def get_type_values_properties(start_index, component_types_values):
    # ruff: disable [E501]
    return "\n".join(
        [
            f"""(property "{type_value[0]}" "{type_value[1]}" (id {start_index + index}) (at 0 0 0)
      (effects (font (size 1.27 1.27)) hide)
    )"""
            for index, type_value in enumerate(component_types_values)
        ]
    )
    # ruff: enable [E501]


def update_library(
    library_name,
    symbol_path,
    component_title,
    template_lib_component,
    output_dir,
    skip_existing,
):
    """
    if component is already in library,
    the library will be updated,
    if not already present in library,
    the component will be added at the end
    """

    with open(
        f"{output_dir}/{symbol_path}/{library_name}.kicad_sym", "rb+"
    ) as lib_file:
        pattern = rf'  \(symbol "{component_title}" (\n|.)*?\n  \)'
        file_content = lib_file.read().decode()

        if f'symbol "{component_title}"' in file_content:
            if skip_existing:
                logging.info(
                    f"component {component_title} already in symbols library, skipping"
                )
                return
            # use regex to find the old component template in the file and
            # replace it with the new one
            logging.info(
                f"found component already in {library_name}, updating {library_name}"
            )
            sub = re.sub(
                pattern=pattern,
                repl=template_lib_component,
                string=file_content,
                flags=re.DOTALL,
                count=1,
            )
            lib_file.seek(0)
            # delete the file content and rewrite it
            lib_file.truncate()
            lib_file.write(sub.encode())
        else:
            # move before the library footer and write the component template
            # see https://github.com/TousstNicolas/JLC2KiCad_lib/issues/46
            new_content = file_content[: file_content.rfind(")")]
            new_content = new_content + template_lib_component + template_lib_footer
            lib_file.seek(0)
            lib_file.write(new_content.encode())
