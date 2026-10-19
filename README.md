# opencodezen4hermes

Paket **overlay** untuk [defaver04/hermes-webui-agent](https://github.com/defaver04/hermes-webui-agent) yang menambahkan sembilan model gratis **OpenCode Zen** ke menu pemilihan model Hermes WebUI — tanpa API key. Repo ini berisi tiga bagian yang saling bergantung: sebuah *sidecar* Python (proxy OpenAI-compatible lokal di `127.0.0.1:8799` yang membentuk ulang setiap request agar lolos gerbang lane anonim Zen), sebuah *provider plugin* Hermes yang mendaftarkan `ProviderProfile` bermuara ke sidecar itu, dan sebuah *config seed* yang menulis route `providers.opencode-zen-free` ke `$HERMES_HOME/config.yaml` supaya picker mau menampilkan slug tersebut. Ketiganya wajib ada: plugin saja menghasilkan grup model kosong, karena `api/config.py` hanya mengizinkan provider plugin masuk `detected_providers` bila sudah punya kredensial atau punya route di config.

## Mulai cepat (pack yang sudah jadi)

Paket arm64-nya diunduh dari halaman [Releases](https://github.com/defaver04/opencodezen4hermes/releases) (sekitar 210 MB).

```bash
mkdir -p ~/hermes-pack
tar -xzf hermes-webui-agent-*-linux-arm64.tar.gz -C ~/hermes-pack
cd ~/hermes-pack
./bin/hermes-webui          # WebUI di http://localhost:8789
```

Kalau mau memeriksa unduhannya, taruh tarball dan `SHA256SUMS` di satu folder lalu jalankan `sha256sum -c --strict SHA256SUMS`. Flag `--strict` bukan hiasan: tanpa itu `sha256sum` tetap keluar dengan kode 0 meski file yang dicatat tidak ada, jadi `SHA256SUMS` yang rusak lolos tanpa suara. Rilis yang dibangun sebelum `build.sh:266` diperbaiki masih mencatat prefiks `dist/` di dalam file itu — lihat [docs/INSTALL.md](docs/INSTALL.md) untuk cara membereskannya.

Launcher `bin/hermes-webui` menjalankan tiga langkah sebelum `exec` ke WebUI, semuanya best-effort dan tercatat di `$STATE/launcher.log`:

1. cek `http://127.0.0.1:8799/healthz`; kalau tidak menjawab, jalankan `bin/opencodezen4hermes-sidecar` detached lalu tunggu sebentar,
2. jalankan `install/seed_config.py` (idempoten — entry yang sudah ada tidak pernah ditimpa),
3. `exec` WebUI persis seperti upstream.

Setelah halaman terbuka, grup **OpenCode Zen (free)** muncul di menu model dengan sembilan model di bawah. Kalau grupnya tidak muncul, lihat [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md).

## Tiga bagian

```
  Hermes WebUI / Agent
        │  openai SDK, base_url = http://127.0.0.1:8799/v1
        ▼
  ┌─────────────────────────────────────────────┐
  │ sidecar/          (python3 -m sidecar)      │
  │  gate.py    stream:true + tool "read" + UA  │
  │  ids.py     ses_<12 hex><14 base62>         │
  │  catalog.py roster gratis dari models.dev   │
  └─────────────────────────────────────────────┘
        │  HTTPS, header & body berbentuk CLI opencode
        ▼
  https://opencode.ai/inference/v1     (lane anonim, tanpa key)

  plugin/opencode-zen-free/  ── mendaftarkan ProviderProfile + roster fallback
  install/seed_config.py     ── menulis route providers.opencode-zen-free
```

## Sembilan model gratis

Diambil dari `GET http://127.0.0.1:8799/v1/models` (sidecar yang sedang jalan). Urutan ini juga urutan di picker.

| Model | Context | Max output | Modalitas | Catatan |
|---|---|---|---|---|
| `beam-free` | 1 048 576 | 131 072 | text | Context terbesar di roster; jawab "pong" pada probe streaming. |
| `big-pickle` | 200 000 | 32 000 | text | Model reasoning; probe 200 tetapi mengeluarkan prefiks `</think>`. |
| `deepseek-v4.1-flash-free` | 200 000 | 32 000 | text, image | Default `default_aux_model`; jawab "pong", TTFT ~2 detik. |
| `glm-5.3-flash-free` | 200 000 | 32 000 | text, image, video, pdf | Satu-satunya yang menerima PDF; jawab "pong". |
| `longcat-2.5-preview-free` | 1 000 000 | 131 072 | text, image | Reasoning; pada `max_tokens` 32 teksnya kosong, normal pada budget wajar. |
| `mimo-v2.6-flash-free` | 1 048 576 | 32 000 | text, image, audio, video | Window 1 MiB dari override lokal (`LOCAL_MODEL_OVERRIDES`), models.dev menulis 200k. |
| `minimax-m3.1-flash-free` | 1 000 000 | 512 000 | text, image, video | Max output terbesar; jawab "pong". |
| `nemotron-3.5-lightning-free` | 262 144 | 262 144 | text | Konteks dan output sama besar; kosong pada budget 32 token. |
| `nemotron-3.5-super-vl-free` | 1 048 576 | 131 072 | text, image | VLM; kosong pada budget 32 token, normal pada budget wajar. |

Roster ini tidak di-hardcode: `sidecar/catalog.py` mengambil daftar live dari lane, memotongnya dengan harga `models.dev` (gratis berarti `input == 0 and output == 0`), lalu membuang `FREE_LANE_BLOCKLIST`. Penjelasan lengkap ada di [docs/MODELS.md](docs/MODELS.md).

## Environment

Semua variabel dibaca `sidecar/config.py` (`Config.from_env`), dan flag CLI menang atas env.

| Var | Default | Arti |
|---|---|---|
| `OPENCODEZEN4HERMES_HOST` | `127.0.0.1` | Alamat bind sidecar. |
| `OPENCODEZEN4HERMES_PORT` | `8799` | Port sidecar; dipakai juga oleh `base_url` di config seed. |
| `OPENCODEZEN4HERMES_LANE_BASE` | `https://opencode.ai/inference/v1` | Base URL lane upstream. |
| `OPENCODEZEN4HERMES_USER_AGENT` | UA bawaan `opencode/1.19.3 ai-sdk/provider-utils/4.0.23 runtime/bun/1.3.14` | Menimpa wire UA; hanya prefiks `opencode/<semver>` yang divalidasi lane (`opencode/1` ditolak, `opencode/1.19.3` lolos). |
| `OPENCODEZEN4HERMES_TIMEOUT` | `300` | Detik inaktivitas socket sebelum panggilan upstream ditinggalkan. |
| `OPENCODEZEN4HERMES_CONNECT_TIMEOUT` | `15` | Detik untuk TCP connect saja. |
| `OPENCODEZEN4HERMES_CATALOG_TTL` | `300` | TTL cache katalog model, dalam detik. |
| `OPENCODEZEN4HERMES_CACHE_DIR` | `$XDG_CACHE_HOME/opencodezen4hermes` | Lokasi cache harga `models.dev`. |
| `OPENCODEZEN4HERMES_VERBOSE` | `0` | Log setiap request yang diproksikan. |
| `OPENCODEZEN4HERMES_INCLUDE_PAID` | `0` | Ikut tampilkan model berbayar di `/v1/models` (untuk debugging). |

Flag yang tersedia: `--host`, `--port`, `--lane-base`, `--catalog-ttl`, `--include-paid`, `--verbose`, `--print-config`.

## Cara memverifikasi

```bash
# 1. Diagnosa mesin: python, /healthz, jumlah model, plugin, config seed, layout pack
python3 scripts/doctor.py
python3 scripts/doctor.py --pack ~/hermes-pack

# 2. Bukti end-to-end ke lane asli (bukan lewat sidecar), per model
python3 scripts/verify_live.py
python3 scripts/verify_live.py --model deepseek-v4.1-flash-free --max-tokens 300

# 3. Cek sidecar secara langsung
curl -s http://127.0.0.1:8799/healthz
curl -s http://127.0.0.1:8799/v1/models
```

`scripts/doctor.py` mencetak baris `PASS`/`WARN`/`FAIL` dengan satu baris saran perbaikan, dan keluar dengan status 1 hanya bila ada `FAIL`. `scripts/verify_live.py` mencetak tabel per model (`works`/`empty`/`rejected`/`error`), mengalirkan SSE sungguhan, dan keluar 0 hanya bila minimal satu model menghasilkan teks tidak kosong.

## Batasan yang jujur

Bagian ini bukan formalitas. Baca sebelum Anda bergantung pada lane ini.

- **Lane-nya tidak terdokumentasi dan tidak stabil.** `https://opencode.ai/inference/v1` adalah jalur anonim milik CLI OpenCode, bukan API publik dengan kontrak. Gerbangnya sudah berubah setidaknya sekali selama pengerjaan repo ini (docs/GATE.md mencatat klaim upstream yang ternyata tidak lagi berlaku). Lane ini bisa berubah atau ditutup tanpa pemberitahuan, dan repo ini tidak punya kewajiban apa pun untuk mengikutinya.
- **Rate limit dihitung per egress IP.** Projek asal (`opencodezen4dsh`) mengirim seluruh subsistem proxy pool karena lane anonim Zen membatasi per IP keluar. Konsekuensi praktisnya: `nemotron-3.5-lightning-free` butuh 40,5 detik saat diprobe berurutan setelah delapan model lain di [docs/E2E_PROOF.md](docs/E2E_PROOF.md), dan itu efek antrean per-IP, bukan properti model. Di IP yang sibuk (NAT kantor, VPN bersama, IP datacenter) wajar bila Anda menerima 403; `GET /v1/models` tetap 200 pada IP yang sama, jadi 403 di POST adalah jawaban lane, bukan bug di repo ini.
  Satu koreksi penting terhadap pembacaan awal kami sendiri: 403 yang pertama kami lihat dari IP datacenter **bukan** karena IP-nya, melainkan karena probe kami tidak mengirim `stream:true` (lihat [docs/GATE.md](docs/GATE.md) — probe non-streaming adalah tes gerbang yang tidak sah). Setelah `stream:true` dikirim, lane yang sama menjawab 200 untuk kesembilan model dari IP datacenter ini.
- **Membaca jawaban wajib streaming.** `stream: false` selalu ditolak `403 FreeTierError`, dan `stream` yang tidak ada juga ditolak. Sidecar memaksa `stream: true` pada setiap request, jadi klien yang tidak bisa menangani SSE tidak akan pernah mendapat jawaban.
- **Repo ini sengaja tidak mengirim proxy pool.** Proyek asal (`opencodezen4dsh`) merotasi IP untuk menghindari limit per-IP. Port ini memilih untuk tidak melakukannya dan mendokumentasikan akibatnya alih-alih menyembunyikannya — ini non-goal yang tercatat di `docs/BLUEPRINT.md` §9, bukan kekurangan yang belum dikerjakan.
- **`jev-1.13-free` dikecualikan.** Model itu menolak bentuk chat-completions dan menjawab `400 ModelProtocolUnsupported`; ia ada di blocklist supaya tidak pernah muncul di picker.
- **Model reasoning bisa menjawab kosong pada `max_tokens` rendah.** Pada budget 32 token beberapa model menghabiskan seluruh kuota untuk token tersembunyi. Pakai `--max-tokens 300` atau lebih saat memverifikasi; kosong di situ bukan model rusak.
- **Verifikasi end-to-end dilakukan manual, bukan di CI.** CI memeriksa resep gerbang terhadap fixture; bukti bahwa lane benar-benar menjawab diambil manusia di egress residensial.

## Lisensi

MIT — lihat [LICENSE](LICENSE). hermes-webui dan hermes-agent tetap di bawah lisensinya masing-masing.
