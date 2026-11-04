# Case V4 "Dual Camera F"

A printable case for OpenFlight: 196 x 100 x 232 mm, 18 printed parts, with room for two cameras and
a side-mounted Noctua NF-A4x20 5V PWM fan.

> [!WARNING]
> Not built yet. The geometry was checked in software only. Physical fit, real pads, screws and
> cables, grip and tripod load, heat and radar behaviour through the front are all still open.
>
> The software in this repository does not use a second camera and does not control the fan.

## Files

| Path | Content |
| --- | --- |
| `stl/` | The 18 print parts in millimetres, already in print orientation |
| `print/README_DE.md`, `print/MONTAGE_*.md`, `print/PRUEFSTATUS.md` | Overview, assembly and check status (German) |
| `print/PTFE_Zuschnitt_1zu1.svg` | PTFE cut template at 1:1 |
| `preview/` | Renders of the case and the air path |
| `sources/` | The scripts that built and checked the case, and `Drucklagen.json` with the print orientations |

Parts named `*_REMOVE_FOR_USE` are alignment gauges for setting up the cameras. Take them out before
use.

`print/README_DE.md` also mentions a SolidCraft file (`.sc`) and test reports. They are not included.

`sources/` is for reference only and does not run as it is: it needs base meshes (`BasisV4/`,
`BasisModules/`, `BasisE/`) that are not included, because they contain models of third-party
hardware.

## License

GPL-3.0, see [LICENSE](LICENSE). The upstream
[openflight-enclosure](https://github.com/open-flight/openflight-enclosure) uses the same license.
