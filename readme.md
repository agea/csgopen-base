# Eclipse Recoil

*Be kind, reload*

Eclipse Recoil is a multiplayer first-person shooter focused on two-team
Deathmatch, deliberate ground movement, and tactical weapon loadouts. Changes
are introduced in small, testable steps while the original gameplay remains
available as a compatibility profile.

The current scope includes native desktop clients, a dedicated server, and map
conversion tools for bringing community maps into the game. Movement and weapon
settings are still prototype values rather than a frame-perfect recreation of
another title.

## Equipment

Open the loadout menu with **comma (,)**. Choose a primary weapon and keep the
Desert Eagle sidearm, then choose **either an optional Bizon/MP9** or **up to
four HE, smoke and mine slots** in any combination. The HE launcher counts as
a primary with seven rounds. Choices save immediately and apply at respawn.

## Development

Pushes to `master` build and publish client releases for macOS Apple Silicon
and Intel, Linux x86_64 and ARM64, and Windows x86_64. Packages include assets,
runtime libraries and the Eclipse Recoil launcher; dedicated-server binaries
are excluded. See the [release guide](doc/csgopen/releases.md) for downloads,
requirements and workflow details.

The default launcher uses the Eclipse Recoil splash, menu logo and application
icon supplied for this project. The loading artwork keeps its full proportions,
including the title and tagline; loading status remains visible.

See the [Eclipse Recoil development guide](doc/csgopen/README.md) for macOS
setup, build commands, compatibility profiles, map conversion, and the local
dedicated server. The [gameplay reference](doc/csgopen/gameplay.md) documents
settings and engine changes, and the
[validation report](doc/csgopen/validation.md) records executed tests and the
remaining manual checklist.

The native development server reads its first map and rotation from
`config/csgopen/server-maps.cfg`, supports end-of-match voting, and serves
complete custom-map ZIPs to Eclipse Recoil clients automatically. Downloaded
maps are verified and cached in the client profile. See the development guide
for the current loopback HTTP setup and validation results.

## Licensing and attribution

Eclipse Recoil includes third-party engine code and assets under their
respective licenses. Copyright notices, source attribution, trademark notices,
and redistribution terms are retained in [the license](doc/license.txt) and
[the complete license inventory](doc/all-licenses.txt).
