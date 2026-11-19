# ACR stage mod toolkit

> **Work in progress.** These tools come from a working stage pipeline and are being extracted one by one. Expect rough edges and changes between versions.
>
> **Built against Assetto Corsa Rally 0.6 (Early Access).** Game data the tools rely on (pace-note tokens, table formats) was read from that version; a later game update may change it.

Standalone tools for building custom stages for Assetto Corsa Rally (ACR), taken from a real-world stage pipeline (GIS data, onboard videos, Street View into Unreal Engine 5 and ACR). Each tool lives in its own folder, runs on its own and has its own README.

| Tool | What it does |
|---|---|
| [pacenotes-generator](pacenotes-generator/) | Onboard video plus your stage's centre spline gives ACR pace notes. It transcribes the co-driver, matches each call to its corner, learns the crew's note system and grades every corner on ACR's 6 (fast) to 1 / Square / HP scale. |

More tools will follow.

## Requirements

Python 3.10+. Each tool lists its packages in its own `requirements.txt`.

## License

MIT (see [LICENSE](LICENSE)). Assetto Corsa Rally and its data belong to their owners; the token names in `pacenotes-generator/acr_tokens.json` are identifiers read from the game, and no game files are included.

## Related

- [acr-extrastages](https://github.com/nitnerolf420/acr-extrastages): `stage-build` packs a stage into ACR. `pacenotes-generator` uses its corner finder and its pace-note token map.
