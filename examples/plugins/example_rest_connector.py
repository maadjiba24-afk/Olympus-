"""Example custom connector — a template for wiring Olympus to your own API.

Custom plugins are the simplest way to give Olympus a new capability without
MCP. They are plain Python functions registered with @plugin; they run locally
and work on EVERY backend (Anthropic and OpenAI-compatible).

To enable: point OLYMPUS_PLUGINS_DIR at this folder, or copy this file into a
`plugins/` directory next to the project. Then `python -m olympus connectors`
will list it and the assigned specialists can call it.

Security:
- `action=False` (default) marks a connector as DATA (read-only). Its output is
  treated as untrusted external content (enveloped, sanitized before memory).
- `action=True` marks it as ACTION (it changes the world). Action connectors
  are automatically stripped from any specialist run that also ingests
  external content — assign them to a non-web specialist like Chronos.
"""

import json
import urllib.parse
import urllib.request

from olympus.connectors import plugin


@plugin(
    name="get_crypto_price",
    description="Get the current USD price of a cryptocurrency by its CoinGecko "
                "id (e.g. 'bitcoin', 'ethereum'). Read-only.",
    schema={
        "type": "object",
        "properties": {
            "coin_id": {"type": "string",
                        "description": "CoinGecko coin id, e.g. 'bitcoin'"},
        },
        "required": ["coin_id"],
    },
    specialists=["plutus"],   # only the financial specialist gets this tool
    action=False,             # data connector (read-only)
)
def get_crypto_price(coin_id: str) -> str:
    url = ("https://api.coingecko.com/api/v3/simple/price"
           f"?ids={urllib.parse.quote(coin_id)}&vs_currencies=usd")
    req = urllib.request.Request(url, headers={"User-Agent": "olympus"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        data = json.loads(resp.read())
    price = data.get(coin_id, {}).get("usd")
    if price is None:
        return f"No price found for '{coin_id}'. Check the CoinGecko id."
    return f"{coin_id} = ${price:,} USD"
