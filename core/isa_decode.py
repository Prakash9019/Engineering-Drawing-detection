"""
ISA-5.1 Tag Decoder
====================
Decodes engineering instrument tags using ISA-5.1 letter conventions.
Examples:
  PIT-211  → Pressure Indicating Transmitter
  TAHH-212 → Temperature Alarm High High
  FZSC-208 → Flow Position Switch Close
  FE-224   → Flow Element
"""
import re


# First letter = measured variable
ISA_FIRST_LETTER = {
    'A': 'ANALYSIS', 'B': 'BURNER', 'C': 'CONDUCTIVITY', 'D': 'DENSITY',
    'E': 'VOLTAGE', 'F': 'FLOW', 'G': 'GAUGING', 'H': 'HAND', 'I': 'CURRENT',
    'K': 'TIME', 'L': 'LEVEL', 'M': 'MOISTURE', 'P': 'PRESSURE',
    'Q': 'QUANTITY', 'S': 'SPEED', 'T': 'TEMPERATURE',
    'U': 'MULTIVARIABLE', 'V': 'VIBRATION', 'W': 'WEIGHT',
    'X': 'UNCLASSIFIED', 'Y': 'EVENT/STATE', 'Z': 'POSITION',
}

# Single-letter function modifiers
ISA_FUNCTION = {
    'I': 'INDICATING', 'T': 'TRANSMITTER', 'E': 'ELEMENT', 'V': 'VALVE',
    'W': 'WELL/THERMOWELL', 'Y': 'RELAY/CONVERTER', 'A': 'ALARM',
    'H': 'HIGH', 'L': 'LOW', 'S': 'SWITCH', 'C': 'CONTROLLER',
    'D': 'DIFFERENTIAL', 'R': 'RECORDER', 'X': 'UNCLASSIFIED',
    'G': 'GAUGE/GLASS', 'K': 'CONTROL STATION',
}

# Two-letter function combos (checked first)
ISA_COMBO = {
    'IT':  'INDICATING TRANSMITTER',
    'AH':  'ALARM HIGH',
    'AL':  'ALARM LOW',
    'HH':  'ALARM HIGH HIGH',
    'LL':  'ALARM LOW LOW',
    'SC':  'SWITCH CLOSE',
    'SO':  'SWITCH OPEN',
    'ZT':  'POSITION TRANSMITTER',
    'ZY':  'SOLENOID/CONVERTER',
    'IC':  'INDICATING CONTROLLER',
    'CV':  'CONTROL VALVE',
    'AHH': 'ALARM HIGH HIGH',
    'ALL': 'ALARM LOW LOW',
}

# Valve type abbreviations
VALVE_TYPES = {
    'BV':  'BALL VALVE',
    'GV':  'GATE VALVE',
    'RV':  'RELIEF VALVE',
    'NRV': 'NON-RETURN VALVE',
    'GLV': 'GLOBE VALVE',
    'FCV': 'FLOW CONTROL VALVE',
    'PCV': 'PRESSURE CONTROL VALVE',
    'TCV': 'TEMPERATURE CONTROL VALVE',
    'LCV': 'LEVEL CONTROL VALVE',
}


def decode_isa(tag: str) -> dict:
    """
    Decode an ISA tag into structured components.

    Returns:
        dict with keys: type, measured, function, number, description
    """
    if not tag:
        return {'type': 'UNKNOWN', 'description': ''}

    raw = tag.upper().strip()

    # Equipment tags FIRST (before any prefix-stripping), since K-V-201 etc
    # have a structure that looks like prefix + tag to the stripper.
    # Patterns: K-V-201, KG-V-201, KM-V-201, S-V-204, V-V-201
    m_equip = re.match(r'^([A-Z]{1,3})-V-(\d{3})([A-Z]?)$', raw)
    if m_equip:
        return {
            'type': 'EQUIPMENT',
            'measured': '',
            'function': '',
            'number': m_equip.group(2),
            'description': raw,
        }

    # Now strip the unit prefix (V-, A-, etc.) for non-equipment tags.
    # Lookahead matches a tag-like body: 1+ letters followed by optional hyphen + digit.
    clean = re.sub(r'^[A-Z0-9]+-(?=[A-Z]+[-]?\d)', '', raw)

    # Valve tags: BV-2243, GV-911, RV-207
    m_valve = re.match(r'^(BV|GV|RV|NRV|GLV|FCV|PCV|TCV|LCV)-?(\d{3,5})([A-Z]?)$', clean)
    if m_valve:
        vtype, num, suf = m_valve.groups()
        return {
            'type': 'VALVE',
            'measured': '',
            'function': VALVE_TYPES.get(vtype, 'VALVE'),
            'number': num + suf,
            'description': VALVE_TYPES.get(vtype, 'VALVE'),
        }

    # Line numbers: 2"-ETH-V057-61440X
    if re.match(r'^\d{1,2}["\']?-', clean):
        return {
            'type': 'PIPING',
            'measured': '',
            'function': '',
            'number': '',
            'description': f'PIPING,{clean}',
        }

    # Logic interlock: I-001, I-004
    m_logic = re.match(r'^I-?(\d{3})([A-Z]?)$', clean)
    if m_logic:
        return {
            'type': 'LOGIC',
            'measured': '',
            'function': 'INTERLOCK',
            'number': m_logic.group(1) + m_logic.group(2),
            'description': 'INTERLOCK/LOGIC ELEMENT',
        }

    # Standard ISA instrument: letters + number
    m_inst = re.match(r'^([A-Z]+?)[-]?(\d{2,5}[A-Z]?)$', clean)
    if not m_inst:
        return {'type': 'UNKNOWN', 'description': tag}

    letters, number = m_inst.groups()
    measured = ISA_FIRST_LETTER.get(letters[0], letters[0])

    # Parse remaining letters (functions/modifiers)
    funcs = []
    i = 1
    while i < len(letters):
        # Try 3-letter combo first (AHH, ALL)
        if i + 2 < len(letters) and letters[i:i+3] in ISA_COMBO:
            funcs.append(ISA_COMBO[letters[i:i+3]])
            i += 3
        # Then 2-letter combo
        elif i + 1 < len(letters) and letters[i:i+2] in ISA_COMBO:
            funcs.append(ISA_COMBO[letters[i:i+2]])
            i += 2
        # Single letter
        elif letters[i] in ISA_FUNCTION:
            funcs.append(ISA_FUNCTION[letters[i]])
            i += 1
        else:
            i += 1

    func_str = ' '.join(funcs)
    description = f"{measured} {func_str}".strip()

    return {
        'type': 'INSTRUMENT',
        'measured': measured,
        'function': func_str,
        'number': number,
        'description': description,
    }


def classify_discipline(tag: str) -> str:
    """Determine engineering discipline from tag format."""
    t = tag.upper()
    if re.match(r'^(V-)?(K|KG|S)-V-', t):
        return 'MECHANICAL'
    if re.match(r'^(V-)?KM-V-', t):
        return 'ELECTRICAL'
    if re.match(r'^(V-)?V-V-', t):
        return 'MECHANICAL'
    if re.match(r'^(V-)?(BV|GV|RV|NRV|GLV|FCV|PCV|TCV|LCV)-', t):
        return 'MECHANICAL'
    if re.match(r'^\d{1,2}(IN)?[\"-]', t):
        return 'PIPING'
    return 'INSTRUMENTATION'


def cedm_normalize(tag: str) -> str:
    """Canonical Engineering Data Model normalization (for dedup keys)."""
    t = tag.upper().strip()
    t = re.sub(r'[\s\.]+', '-', t)
    t = re.sub(r'-+', '-', t)
    return t.strip('-')
