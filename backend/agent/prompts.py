"""System mandate + per-phase prompts for the read-only real-estate analyst.

Adapted from the portfolio agent: the mandate is a NEUTRAL market analyst — it
reports trends, yields, and notable segments as research signals with caveats, and
**never** gives buy/sell or transaction advice. No trades, no watchlist — the agent
only reads gold/silver via tools and writes a Slovak memo.
"""

MANDATE = """Si neutrálny analytik realitného trhu pre bratislavské byty. Tvojou
úlohou je sledovanie a interpretácia trhu — NIE investičné poradenstvo.

ROZSAH: byty v 17 mestských častiach Bratislavy + 4 prímestské obce (Stupava,
Marianka, Borinka, Lozorno); predaj aj prenájom; granularita mestská časť / okres.

MANDÁT:
- Reportuješ trendy €/m², inventár, hrubé výnosy (yield) a nápadné segmenty ako
  VÝSKUMNÉ SIGNÁLY S VÝHRADAMI.
- NIKDY nedávaš odporúčania kúpiť/predať/prenajať ani žiadne transakčné rady.
- Pri každom signáli uvedieš výhrady: veľkosť vzorky, pokrytie dát, a koľko
  záznamov bolo vylúčených a prečo.
- Segmenty s nízkou vzorkou (low_confidence) alebo krátkou históriou explicitne
  označíš ako neisté. Týždeň 1 nemá medzitýždňové porovnanie — to NIE je nula,
  je to „údaj nedostupný".

DÁTA: Všetky čísla získavaš VÝLUČNE cez nástroje (tools) — nehádaj a nepočítaj
čísla sám. Aritmetika je už hotová v dátach; ty interpretuješ. Jazyk: slovenčina.

VÝSTUP: stručné, vecné intelligence memo. Žiadne rady na konanie — len pozorovania,
kontext a otvorené otázky pre ďalší výskum."""


def screening_user_prompt(segment_table: list[dict], grain: str, week: str | None) -> str:
    """Level 1 — screen the compact gold segment table, pick segments to deep-dive."""
    if segment_table:
        lines = []
        for r in segment_table:
            area = r.get("city") or r.get("district") or "?"
            wow = r.get("ppm2_wow_pct")
            wow_s = "n/a" if wow is None else f"{wow:+.1f}%"
            lc = " [low_conf]" if r.get("low_confidence") else ""
            lines.append(
                f"- {area} | {r.get('category')} | {r.get('deal')}: "
                f"median_ppm2={r.get('median_ppm2')}, n={r.get('listing_count')}, "
                f"WoW={wow_s}{lc}"
            )
        table = "\n".join(lines)
    else:
        table = "(žiadne segmenty — dáta sa ešte len zbierajú)"

    return (
        f"Najnovší týždeň: {week or 'n/a'} · granularita: {grain}.\n"
        f"Kompaktná tabuľka segmentov (segment_weekly, top podľa počtu inzerátov):\n"
        f"{table}\n\n"
        "Úloha: vyber 2–4 NAJ­NÁPADNEJŠIE segmenty na hĺbkovú analýzu — také, kde "
        "vidíš pohyb €/m², divergenciu, nezvyčajný inventár alebo zaujímavý yield. "
        "Vyhýbaj sa segmentom s nízkou vzorkou, pokiaľ nie sú samy o sebe signálom.\n"
        'Odpovedz IBA JSON: {"selected": [{"area": "...", "category": "...", '
        '"deal": "predaj|prenajom", "reason": "1 veta prečo"}], '
        '"rationale": "stručné zhrnutie výberu"}.'
    )


def deepdive_user_prompt(selected: list[dict], grain: str) -> str:
    """Level 2 — deep-dive the selected segments via tools, then write the memo."""
    picks = "\n".join(
        f"- {s.get('area')} | {s.get('category')} | {s.get('deal')} "
        f"({s.get('reason', '')})"
        for s in selected
    ) or "(žiadne)"
    return (
        f"Hĺbková analýza vybraných segmentov (granularita: {grain}):\n{picks}\n\n"
        "Pre každý segment si VYŽIADAJ dáta cez nástroje (môžeš volať viac naraz):\n"
        "- segment_stats — týždenné mediány €/m², vzorka, WoW/MoM,\n"
        "- trend_series — časový rad zvolenej metriky,\n"
        "- yield_analysis — hrubý výnos (predaj vs prenájom) pre danú oblasť×kategóriu,\n"
        "- query_listings — vzorka konkrétnych inzerátov (max 150) pre kontext.\n\n"
        "Potom napíš INTELLIGENCE MEMO v slovenčine ako voľný text:\n"
        "1) Kľúčové pozorovania po segmentoch (€/m², inventár, WoW/MoM kontext).\n"
        "2) Yield / rent-vs-buy signály, kde sú dáta dostatočné.\n"
        "3) Výhrady: veľkosť vzorky, pokrytie, vylúčené záznamy, krátka história.\n"
        "4) Otvorené otázky pre ďalší výskum.\n"
        "NEPÍŠ žiadne odporúčania kúpiť/predať. Len neutrálne signály a kontext."
    )
