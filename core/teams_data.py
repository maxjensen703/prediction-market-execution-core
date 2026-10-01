"""Team registry data for core/teams.py — the canonical name dicts only, no logic."""

# ── Canonical team registry ────────────────────────────────────────────────
# Format: "any known variant (lowercased, no punctuation)": "Canonical Name"
#
# Rules for canonical names:
# - Short form only: "Chiefs" not "Kansas City Chiefs"
# - Title case: "49ers" not "49ERS"
# - No city prefix unless the city IS the name: "Heat" not "Miami Heat",
#   but "Paris Saint-Germain" -> "PSG" for brevity
#
# When adding new entries: add ALL known variants you have encountered,
# including abbreviations, city names alone, and common misspellings.
#
# City-only variants ("boston", "miami") are inherently ambiguous across
# sports — those live in SPORT_OVERRIDES below, which wins over this dict.

TEAM_REGISTRY: dict[str, str] = {

    # ── NFL ──────────────────────────────────────────────────────────────
    # AFC East
    "buffalo bills": "Bills",
    "buffalo": "Bills",
    "bills": "Bills",
    "buf": "Bills",

    "miami dolphins": "Dolphins",
    "miami": "Dolphins",
    "dolphins": "Dolphins",
    "mia": "Dolphins",

    "new england patriots": "Patriots",
    "new england": "Patriots",
    "patriots": "Patriots",
    "ne": "Patriots",
    "nep": "Patriots",

    "new york jets": "Jets",
    "ny jets": "Jets",
    "jets": "Jets",
    "nyj": "Jets",

    # AFC North
    "baltimore ravens": "Ravens",
    "baltimore": "Ravens",
    "ravens": "Ravens",
    "bal": "Ravens",

    "cincinnati bengals": "Bengals",
    "cincinnati": "Bengals",
    "bengals": "Bengals",
    "cin": "Bengals",

    "cleveland browns": "Browns",
    "cleveland": "Browns",
    "browns": "Browns",
    "cle": "Browns",

    "pittsburgh steelers": "Steelers",
    "pittsburgh": "Steelers",
    "steelers": "Steelers",
    "pit": "Steelers",

    # AFC South
    "houston texans": "Texans",
    "houston": "Texans",
    "texans": "Texans",
    "hou": "Texans",

    "indianapolis colts": "Colts",
    "indianapolis": "Colts",
    "colts": "Colts",
    "ind": "Colts",

    "jacksonville jaguars": "Jaguars",
    "jacksonville": "Jaguars",
    "jaguars": "Jaguars",
    "jax": "Jaguars",
    "jac": "Jaguars",

    "tennessee titans": "Titans",
    "tennessee": "Titans",
    "titans": "Titans",
    "ten": "Titans",

    # AFC West
    "denver broncos": "Broncos",
    "denver": "Broncos",
    "broncos": "Broncos",
    "den": "Broncos",

    "kansas city chiefs": "Chiefs",
    "kansas city": "Chiefs",
    "chiefs": "Chiefs",
    "kc": "Chiefs",
    "kcc": "Chiefs",

    "las vegas raiders": "Raiders",
    "las vegas": "Raiders",
    "oakland raiders": "Raiders",
    "raiders": "Raiders",
    "lv": "Raiders",
    "lvr": "Raiders",

    "los angeles chargers": "Chargers",
    "la chargers": "Chargers",
    "chargers": "Chargers",
    "lac": "Chargers",

    # NFC East
    "dallas cowboys": "Cowboys",
    "dallas": "Cowboys",
    "cowboys": "Cowboys",
    "dal": "Cowboys",

    "new york giants": "Giants",
    "ny giants": "Giants",
    "giants": "Giants",
    "nyg": "Giants",

    "philadelphia eagles": "Eagles",
    "philadelphia": "Eagles",
    "eagles": "Eagles",
    "phi": "Eagles",

    "washington commanders": "Commanders",
    "washington": "Commanders",
    "commanders": "Commanders",
    "washington football team": "Commanders",
    "washington redskins": "Commanders",
    "was": "Commanders",
    "wsh": "Commanders",

    # NFC North
    "chicago bears": "Bears",
    "chicago": "Bears",
    "bears": "Bears",
    "chi": "Bears",

    "detroit lions": "Lions",
    "detroit": "Lions",
    "lions": "Lions",
    "det": "Lions",

    "green bay packers": "Packers",
    "green bay": "Packers",
    "packers": "Packers",
    "gb": "Packers",
    "gnb": "Packers",

    "minnesota vikings": "Vikings",
    "minnesota": "Vikings",
    "vikings": "Vikings",
    "min": "Vikings",

    # NFC South
    "atlanta falcons": "Falcons",
    "atlanta": "Falcons",
    "falcons": "Falcons",
    "atl": "Falcons",

    "carolina panthers": "Panthers",
    "carolina": "Panthers",
    "panthers": "Panthers",
    "car": "Panthers",

    "new orleans saints": "Saints",
    "new orleans": "Saints",
    "saints": "Saints",
    "no": "Saints",
    "nor": "Saints",

    "tampa bay buccaneers": "Buccaneers",
    "tampa bay": "Buccaneers",
    "buccaneers": "Buccaneers",
    "bucs": "Buccaneers",
    "tb": "Buccaneers",
    "tam": "Buccaneers",

    # NFC West
    "arizona cardinals": "Cardinals",
    "arizona": "Cardinals",
    "cardinals": "Cardinals",
    "ari": "Cardinals",

    "los angeles rams": "Rams",
    "la rams": "Rams",
    "rams": "Rams",
    "lar": "Rams",

    "san francisco 49ers": "49ers",
    "san francisco": "49ers",
    "49ers": "49ers",
    "sf": "49ers",
    "sfo": "49ers",

    "seattle seahawks": "Seahawks",
    "seattle": "Seahawks",
    "seahawks": "Seahawks",
    "sea": "Seahawks",

    # ── NBA ──────────────────────────────────────────────────────────────
    # Atlantic
    "boston celtics": "Celtics",
    "boston": "Celtics",
    "celtics": "Celtics",
    "bos": "Celtics",

    "brooklyn nets": "Nets",
    "brooklyn": "Nets",
    "nets": "Nets",
    "bkn": "Nets",
    "brk": "Nets",

    "new york knicks": "Knicks",
    "new york": "Knicks",
    "knicks": "Knicks",
    "nyk": "Knicks",
    "ny knicks": "Knicks",

    "philadelphia 76ers": "76ers",
    "76ers": "76ers",
    "sixers": "76ers",
    "phi 76ers": "76ers",
    "phila": "76ers",

    "toronto raptors": "Raptors",
    "toronto": "Raptors",
    "raptors": "Raptors",
    "tor": "Raptors",

    # Central
    "chicago bulls": "Bulls",
    "bulls": "Bulls",
    "chi bulls": "Bulls",

    "cleveland cavaliers": "Cavaliers",
    "cavaliers": "Cavaliers",
    "cavs": "Cavaliers",
    "cle cavaliers": "Cavaliers",

    "detroit pistons": "Pistons",
    "pistons": "Pistons",
    "det pistons": "Pistons",

    "indiana pacers": "Pacers",
    "indiana": "Pacers",
    "pacers": "Pacers",
    "ind pacers": "Pacers",

    "milwaukee bucks": "Bucks",
    "milwaukee": "Bucks",
    "bucks": "Bucks",
    "mil": "Bucks",

    # Southeast
    "atlanta hawks": "Hawks",
    "hawks": "Hawks",
    "atl hawks": "Hawks",

    "charlotte hornets": "Hornets",
    "charlotte": "Hornets",
    "hornets": "Hornets",
    "cha": "Hornets",

    "miami heat": "Heat",
    "heat": "Heat",
    "mia heat": "Heat",

    "orlando magic": "Magic",
    "orlando": "Magic",
    "magic": "Magic",
    "orl": "Magic",

    "washington wizards": "Wizards",
    "wizards": "Wizards",
    "was wizards": "Wizards",

    # Northwest
    "denver nuggets": "Nuggets",
    "nuggets": "Nuggets",
    "den nuggets": "Nuggets",

    "minnesota timberwolves": "Timberwolves",
    "timberwolves": "Timberwolves",
    "wolves": "Timberwolves",
    "min timberwolves": "Timberwolves",

    "oklahoma city thunder": "Thunder",
    "oklahoma city": "Thunder",
    "thunder": "Thunder",
    "okc": "Thunder",

    "portland trail blazers": "Blazers",
    "portland": "Blazers",
    "trail blazers": "Blazers",
    "blazers": "Blazers",
    "por": "Blazers",

    "utah jazz": "Jazz",
    "utah": "Jazz",
    "jazz": "Jazz",
    "uta": "Jazz",

    # Pacific
    "golden state warriors": "Warriors",
    "golden state": "Warriors",
    "warriors": "Warriors",
    "gsw": "Warriors",
    "gs": "Warriors",

    "los angeles clippers": "Clippers",
    "la clippers": "Clippers",
    "clippers": "Clippers",
    "lac clippers": "Clippers",

    "los angeles lakers": "Lakers",
    "la lakers": "Lakers",
    "lakers": "Lakers",
    "lal": "Lakers",

    "phoenix suns": "Suns",
    "phoenix": "Suns",
    "suns": "Suns",
    "phx": "Suns",
    "pho": "Suns",

    "sacramento kings": "Kings",
    "sacramento": "Kings",
    "kings": "Kings",
    "sac": "Kings",

    # Southwest (added: missing from the original registry draft)
    "san antonio spurs": "Spurs",
    "san antonio": "Spurs",
    "spurs": "Spurs",
    "sas": "Spurs",

    "dallas mavericks": "Mavericks",
    "mavericks": "Mavericks",
    "mavs": "Mavericks",
    "dal mavericks": "Mavericks",

    "houston rockets": "Rockets",
    "rockets": "Rockets",
    "hou rockets": "Rockets",

    "memphis grizzlies": "Grizzlies",
    "memphis": "Grizzlies",
    "grizzlies": "Grizzlies",
    "mem": "Grizzlies",

    "new orleans pelicans": "Pelicans",
    "pelicans": "Pelicans",
    "nop": "Pelicans",

    # ── MLB ──────────────────────────────────────────────────────────────
    "arizona diamondbacks": "Diamondbacks",
    "diamondbacks": "Diamondbacks",
    "dbacks": "Diamondbacks",
    "ari diamondbacks": "Diamondbacks",

    "atlanta braves": "Braves",
    "braves": "Braves",
    "atl braves": "Braves",

    "baltimore orioles": "Orioles",
    "orioles": "Orioles",
    "bal orioles": "Orioles",

    "boston red sox": "Red Sox",
    "red sox": "Red Sox",
    "bos red sox": "Red Sox",

    "chicago cubs": "Cubs",
    "cubs": "Cubs",
    "chc": "Cubs",

    "chicago white sox": "White Sox",
    "white sox": "White Sox",
    "cws": "White Sox",
    "chw": "White Sox",

    "cincinnati reds": "Reds",
    "reds": "Reds",
    "cin reds": "Reds",

    "cleveland guardians": "Guardians",
    "guardians": "Guardians",
    "cleveland indians": "Guardians",
    "cle guardians": "Guardians",

    "colorado rockies": "Rockies",
    "colorado": "Rockies",
    "rockies": "Rockies",
    "col": "Rockies",

    "detroit tigers": "Tigers",
    "tigers": "Tigers",
    "det tigers": "Tigers",

    "houston astros": "Astros",
    "astros": "Astros",
    "hou astros": "Astros",

    "kansas city royals": "Royals",
    "royals": "Royals",
    "kcr": "Royals",

    "los angeles angels": "Angels",
    "la angels": "Angels",
    "angels": "Angels",
    "laa": "Angels",
    "anaheim angels": "Angels",

    "los angeles dodgers": "Dodgers",
    "la dodgers": "Dodgers",
    "dodgers": "Dodgers",
    "lad": "Dodgers",

    "miami marlins": "Marlins",
    "marlins": "Marlins",
    "mia marlins": "Marlins",
    "florida marlins": "Marlins",

    "milwaukee brewers": "Brewers",
    "brewers": "Brewers",
    "mil brewers": "Brewers",

    "minnesota twins": "Twins",
    "twins": "Twins",
    "min twins": "Twins",

    "new york mets": "Mets",
    "mets": "Mets",
    "nym": "Mets",
    "ny mets": "Mets",

    "new york yankees": "Yankees",
    "yankees": "Yankees",
    "nyy": "Yankees",
    "ny yankees": "Yankees",

    "oakland athletics": "Athletics",
    "athletics": "Athletics",
    "oakland as": "Athletics",
    "as": "Athletics",
    "oak": "Athletics",

    "philadelphia phillies": "Phillies",
    "phillies": "Phillies",
    "phi phillies": "Phillies",

    "pittsburgh pirates": "Pirates",
    "pirates": "Pirates",
    "pit pirates": "Pirates",

    "san diego padres": "Padres",
    "san diego": "Padres",
    "padres": "Padres",
    "sd": "Padres",
    "sdp": "Padres",

    "san francisco giants": "Giants",
    "sf giants": "Giants",
    "sfg": "Giants",

    "seattle mariners": "Mariners",
    "mariners": "Mariners",
    "sea mariners": "Mariners",

    "st louis cardinals": "Cardinals",
    "saint louis cardinals": "Cardinals",
    "stl": "Cardinals",
    "st louis": "Cardinals",

    "tampa bay rays": "Rays",
    "rays": "Rays",
    "tb rays": "Rays",

    "texas rangers": "Rangers",
    "texas": "Rangers",
    "rangers": "Rangers",
    "tex": "Rangers",

    "toronto blue jays": "Blue Jays",
    "blue jays": "Blue Jays",
    "tor blue jays": "Blue Jays",
    "tbj": "Blue Jays",

    "washington nationals": "Nationals",
    "nationals": "Nationals",
    "nats": "Nationals",
    "was nationals": "Nationals",
    "wsn": "Nationals",

    # ── NHL ──────────────────────────────────────────────────────────────
    "anaheim ducks": "Ducks",
    "ducks": "Ducks",
    "ana": "Ducks",

    "boston bruins": "Bruins",
    "bruins": "Bruins",
    "bos bruins": "Bruins",

    "buffalo sabres": "Sabres",
    "sabres": "Sabres",
    "buf sabres": "Sabres",

    "calgary flames": "Flames",
    "calgary": "Flames",
    "flames": "Flames",
    "cgy": "Flames",

    "carolina hurricanes": "Hurricanes",
    "hurricanes": "Hurricanes",
    "canes": "Hurricanes",
    "car hurricanes": "Hurricanes",

    "chicago blackhawks": "Blackhawks",
    "blackhawks": "Blackhawks",
    "chi blackhawks": "Blackhawks",

    "colorado avalanche": "Avalanche",
    "avalanche": "Avalanche",
    "avs": "Avalanche",
    "col avalanche": "Avalanche",

    "columbus blue jackets": "Blue Jackets",
    "blue jackets": "Blue Jackets",
    "cbj": "Blue Jackets",

    "dallas stars": "Stars",
    "stars": "Stars",
    "dal stars": "Stars",

    "detroit red wings": "Red Wings",
    "red wings": "Red Wings",
    "det red wings": "Red Wings",

    "edmonton oilers": "Oilers",
    "edmonton": "Oilers",
    "oilers": "Oilers",
    "edm": "Oilers",

    "florida panthers": "Panthers",
    "florida": "Panthers",
    "fla": "Panthers",

    "los angeles kings": "Kings",
    "la kings": "Kings",
    "lak": "Kings",

    "minnesota wild": "Wild",
    "wild": "Wild",
    "min wild": "Wild",

    "montreal canadiens": "Canadiens",
    "montreal": "Canadiens",
    "canadiens": "Canadiens",
    "habs": "Canadiens",
    "mtl": "Canadiens",

    "nashville predators": "Predators",
    "nashville": "Predators",
    "predators": "Predators",
    "preds": "Predators",
    "nsh": "Predators",

    "new jersey devils": "Devils",
    "new jersey": "Devils",
    "devils": "Devils",
    "njd": "Devils",

    "new york islanders": "Islanders",
    "islanders": "Islanders",
    "nyi": "Islanders",
    "ny islanders": "Islanders",

    "new york rangers": "Rangers",
    "nyr": "Rangers",
    "ny rangers": "Rangers",

    "ottawa senators": "Senators",
    "ottawa": "Senators",
    "senators": "Senators",
    "sens": "Senators",
    "ott": "Senators",

    "philadelphia flyers": "Flyers",
    "flyers": "Flyers",
    "phi flyers": "Flyers",

    "pittsburgh penguins": "Penguins",
    "penguins": "Penguins",
    "pens": "Penguins",
    "pit penguins": "Penguins",

    "san jose sharks": "Sharks",
    "san jose": "Sharks",
    "sharks": "Sharks",
    "sjs": "Sharks",

    "seattle kraken": "Kraken",
    "kraken": "Kraken",
    "sea kraken": "Kraken",

    "st louis blues": "Blues",
    "saint louis blues": "Blues",
    "blues": "Blues",
    "stl blues": "Blues",

    "tampa bay lightning": "Lightning",
    "lightning": "Lightning",
    "bolts": "Lightning",
    "tb lightning": "Lightning",

    "toronto maple leafs": "Maple Leafs",
    "maple leafs": "Maple Leafs",
    "leafs": "Maple Leafs",
    "tor maple leafs": "Maple Leafs",

    "utah hockey club": "Utah HC",
    "utah hc": "Utah HC",

    "vancouver canucks": "Canucks",
    "vancouver": "Canucks",
    "canucks": "Canucks",
    "van": "Canucks",

    "vegas golden knights": "Golden Knights",
    "golden knights": "Golden Knights",
    "knights": "Golden Knights",
    "vgk": "Golden Knights",
    "vegas": "Golden Knights",

    "washington capitals": "Capitals",
    "capitals": "Capitals",
    "caps": "Capitals",
    "was capitals": "Capitals",

    "winnipeg jets": "Jets",
    "winnipeg": "Jets",
    "wpg": "Jets",

    # ── WNBA ─────────────────────────────────────────────────────────────
    # Polymarket sends "City Nickname"; Kalshi sends city-only (see WNBA in
    # SPORT_OVERRIDES). 2026 season incl. expansion Tempo (TOR) + Fire (POR).
    "atlanta dream": "Dream",
    "dream": "Dream",

    "chicago sky": "Sky",
    "sky": "Sky",

    "connecticut sun": "Sun",
    "connecticut": "Sun",
    "sun": "Sun",

    "dallas wings": "Wings",
    "wings": "Wings",

    "golden state valkyries": "Valkyries",
    "valkyries": "Valkyries",

    "indiana fever": "Fever",
    "fever": "Fever",

    "las vegas aces": "Aces",
    "aces": "Aces",

    "los angeles sparks": "Sparks",
    "la sparks": "Sparks",
    "sparks": "Sparks",

    "minnesota lynx": "Lynx",
    "lynx": "Lynx",

    "new york liberty": "Liberty",
    "liberty": "Liberty",

    "phoenix mercury": "Mercury",
    "mercury": "Mercury",

    "portland fire": "Fire",
    "fire": "Fire",

    "seattle storm": "Storm",
    "storm": "Storm",

    "toronto tempo": "Tempo",
    "tempo": "Tempo",

    "washington mystics": "Mystics",
    "mystics": "Mystics",

    # ── EPL ──────────────────────────────────────────────────────────────
    "arsenal": "Arsenal",
    "afc": "Arsenal",

    "aston villa": "Aston Villa",
    "villa": "Aston Villa",
    "avfc": "Aston Villa",

    "bournemouth": "Bournemouth",
    "afc bournemouth": "Bournemouth",

    "brentford": "Brentford",
    "brentford fc": "Brentford",

    "brighton": "Brighton",
    "brighton and hove albion": "Brighton",
    "brighton & hove albion": "Brighton",
    "bhafc": "Brighton",

    "chelsea": "Chelsea",
    "cfc": "Chelsea",

    "crystal palace": "Crystal Palace",
    "palace": "Crystal Palace",
    "cpfc": "Crystal Palace",

    "everton": "Everton",
    "efc": "Everton",

    "fulham": "Fulham",
    "fulham fc": "Fulham",

    "ipswich": "Ipswich",
    "ipswich town": "Ipswich",
    "itfc": "Ipswich",

    "leicester": "Leicester",
    "leicester city": "Leicester",
    "lcfc": "Leicester",

    "liverpool": "Liverpool",
    "lfc": "Liverpool",

    "manchester city": "Man City",
    "man city": "Man City",
    "mcfc": "Man City",
    "man c": "Man City",

    "manchester united": "Man United",
    "man united": "Man United",
    "man utd": "Man United",
    "mufc": "Man United",

    "newcastle": "Newcastle",
    "newcastle united": "Newcastle",
    "nufc": "Newcastle",

    "nottingham forest": "Nott'm Forest",
    "notts forest": "Nott'm Forest",
    "forest": "Nott'm Forest",
    "nffc": "Nott'm Forest",

    "southampton": "Southampton",
    "stfc": "Southampton",

    "tottenham": "Tottenham",
    "tottenham hotspur": "Tottenham",
    "thfc": "Tottenham",

    "west ham": "West Ham",
    "west ham united": "West Ham",
    "whufc": "West Ham",

    "wolverhampton": "Wolves",
    "wolverhampton wanderers": "Wolves",
    "wwfc": "Wolves",
}


# ── Sport-scoped overrides (checked BEFORE TEAM_REGISTRY) ──────────────────
# City-only names collide across sports: Kalshi says "Boston" for the Red Sox
# but the flat registry can only map "boston" to one team (Celtics). Fetchers
# know the sport, so these per-sport dicts resolve the ambiguity. Variants
# below were observed in live Kalshi/Polymarket data (June 2026).

SPORT_OVERRIDES: dict[str, dict[str, str]] = {
    # World Cup national teams. Most countries are spelled identically on both venues
    # (Argentina, Austria, France, Brazil, ...) so they pass through and still match
    # exactly; this map only canonicalizes the names that DIVERGE between Kalshi & PM US.
    # Keys are _clean()'d (lowercase, no punctuation). Missing a variant just misses an
    # arb — it can never create a false match (matching is exact on the canonical string).
    "WC": {
        "congo dr": "Congo DR", "dr congo": "Congo DR",
        "democratic republic of the congo": "Congo DR", "democratic republic of congo": "Congo DR",
        "united states": "USA", "usa": "USA", "united states of america": "USA",
        "south korea": "South Korea", "korea republic": "South Korea",
        "republic of korea": "South Korea", "korea": "South Korea",
        "north korea": "North Korea", "korea dpr": "North Korea",
        "ivory coast": "Ivory Coast", "cote divoire": "Ivory Coast", "côte divoire": "Ivory Coast",
        "czechia": "Czechia", "czech republic": "Czechia",
        "cape verde": "Cape Verde", "cabo verde": "Cape Verde",
        "bosnia and herzegovina": "Bosnia", "bosnia": "Bosnia",
        "iran": "Iran", "ir iran": "Iran",
        "netherlands": "Netherlands", "holland": "Netherlands",
        "turkiye": "Turkey", "türkiye": "Turkey", "turkey": "Turkey",
        "united arab emirates": "UAE", "uae": "UAE",
        "north macedonia": "North Macedonia", "macedonia": "North Macedonia",
        "saudi arabia": "Saudi Arabia", "ksa": "Saudi Arabia",
    },
    "MLB": {
        "arizona": "Diamondbacks",
        "atlanta": "Braves",
        "baltimore": "Orioles",
        "boston": "Red Sox",
        "chicago c": "Cubs",
        "chicago ws": "White Sox",
        "cincinnati": "Reds",
        "cleveland": "Guardians",
        "detroit": "Tigers",
        "houston": "Astros",
        "kansas city": "Royals",
        "los angeles a": "Angels",
        "los angeles d": "Dodgers",
        "miami": "Marlins",
        "milwaukee": "Brewers",
        "minnesota": "Twins",
        "new york m": "Mets",
        "new york y": "Yankees",
        "philadelphia": "Phillies",
        "pittsburgh": "Pirates",
        "san francisco": "Giants",
        "seattle": "Mariners",
        "tampa bay": "Rays",
        "toronto": "Blue Jays",
        "washington": "Nationals",
    },
    "NBA": {
        "dallas": "Mavericks",
        "houston": "Rockets",
        "new orleans": "Pelicans",
        "washington": "Wizards",
    },
    "NFL": {
        "los angeles c": "Chargers",
        "los angeles r": "Rams",
        "new york g": "Giants",
        "new york j": "Jets",
    },
    "NHL": {
        # Kalshi sends city-only names; the flat registry maps most of these
        # cities to the wrong sport (boston->Celtics, toronto->Raptors, etc.),
        # so every NHL city is pinned here. "new york" is intentionally absent
        # (Rangers AND Islanders) — those arrive as "Rangers"/"Islanders".
        "anaheim": "Ducks",
        "boston": "Bruins",
        "buffalo": "Sabres",
        "calgary": "Flames",
        "carolina": "Hurricanes",
        "chicago": "Blackhawks",
        "colorado": "Avalanche",
        "columbus": "Blue Jackets",
        "dallas": "Stars",
        "detroit": "Red Wings",
        "edmonton": "Oilers",
        "florida": "Panthers",
        "los angeles": "Kings",
        "minnesota": "Wild",
        "montreal": "Canadiens",
        "nashville": "Predators",
        "new jersey": "Devils",
        "ottawa": "Senators",
        "philadelphia": "Flyers",
        "pittsburgh": "Penguins",
        "san jose": "Sharks",
        "seattle": "Kraken",
        "st louis": "Blues",
        "tampa bay": "Lightning",
        "toronto": "Maple Leafs",
        "vancouver": "Canucks",
        "vegas": "Golden Knights",
        "las vegas": "Golden Knights",
        "washington": "Capitals",
        "winnipeg": "Jets",
        "utah": "Utah HC",
    },
    "WNBA": {
        # Kalshi sends city-only WNBA names; each city has exactly one team.
        "atlanta": "Dream",
        "chicago": "Sky",
        "connecticut": "Sun",
        "dallas": "Wings",
        "golden state": "Valkyries",
        "indiana": "Fever",
        "las vegas": "Aces",
        "los angeles": "Sparks",
        "minnesota": "Lynx",
        "new york": "Liberty",
        "phoenix": "Mercury",
        "portland": "Fire",
        "seattle": "Storm",
        "toronto": "Tempo",
        "washington": "Mystics",
    },
}
