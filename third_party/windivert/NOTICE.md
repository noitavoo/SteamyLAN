# WinDivert

SteamyLAN redistributes the official x64 binary release of WinDivert 2.2.2,
variant A, from <https://reqrypt.org/windivert.html>.

WinDivert is dynamically loaded only by SteamyLAN's elevated LAN-discovery
helper. It is licensed under the GNU Lesser General Public License version 3
or, at your option, the GNU General Public License version 2. The complete
upstream license is included in `LICENSE.txt`; upstream source is available at
<https://github.com/basil00/WinDivert>.

Pinned SHA-256 hashes:

- `WinDivert.dll`: `c1e060ee19444a259b2162f8af0f3fe8c4428a1c6f694dce20de194ac8d7d9a2`
- `WinDivert64.sys`: `8da085332782708d8767bcace5327a6ec7283c17cfb85e40b03cd2323a90ddc2`

The official driver is Authenticode-signed. SteamyLAN verifies both file
hashes before requesting elevation and again inside the elevated helper.
