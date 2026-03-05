from flask import Flask, request, jsonify, send_file
import subprocess, requests, os, uuid
from pathlib import Path

def install_ffmpeg():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except FileNotFoundError:
        os.system("apt-get update -y && apt-get install -y ffmpeg")

install_ffmpeg()

app = Flask(__name__)
OUTPUT_DIR = Path("/tmp/outputs")
OUTPUT_DIR.mkdir(exist_ok=True)


def download(url: str, path: str):
    """Download file from URL."""
    r = requests.get(url, timeout=120)
    r.raise_for_status()
    with open(path, 'wb') as f:
        f.write(r.content)


def get_duration(path: str) -> float:
    """Get file duration via ffprobe."""
    probe = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
         "-of", "csv=p=0", path],
        capture_output=True, text=True
    )
    try:
        return float(probe.stdout.strip())
    except:
        return 6.0


@app.route('/health')
def health():
    return jsonify({"ok": True})


@app.route('/merge', methods=['POST'])
def merge():
    """
    Accepts JSON:
    {
        "video1": "https://...",         ← кліп 1 (shot_1)
        "video2": "https://...",         ← кліп 2 (shot_2)
        "video3": "https://...",         ← кліп 3 (shot_3)
        "narrator": "https://...",       ← голос (опційно)
        "music": "https://...",          ← фонова музика (опційно)
        "ambient": "https://...",        ← ambient звук (опційно)
        "narrator_start": 6             ← з якої секунди голос (default: 6)
    }
    """
    data = request.json
    uid = str(uuid.uuid4())[:8]

    # ── Шляхи до тимчасових файлів ──────────────────────────────────────
    v1       = f"/tmp/v1_{uid}.mp4"
    v2       = f"/tmp/v2_{uid}.mp4"
    v3       = f"/tmp/v3_{uid}.mp4"
    narrator = f"/tmp/narrator_{uid}.mp3"
    music    = f"/tmp/music_{uid}.mp3"
    ambient  = f"/tmp/ambient_{uid}.mp3"
    merged   = f"/tmp/merged_{uid}.mp4"
    list_f   = f"/tmp/list_{uid}.txt"
    final    = str(OUTPUT_DIR / f"{uid}.mp4")

    try:
        # ── 1. Завантажуємо 3 відео ──────────────────────────────────────
        for url, path in [
            (data['video1'], v1),
            (data['video2'], v2),
            (data['video3'], v3),
        ]:
            download(url, path)

        # ── 2. Склеюємо 3 відео в одне ──────────────────────────────────
        with open(list_f, 'w') as f:
            f.write(f"file '{v1}'\nfile '{v2}'\nfile '{v3}'\n")

        subprocess.run(
            ["ffmpeg", "-y", "-f", "concat", "-safe", "0",
             "-i", list_f, "-c", "copy", merged],
            capture_output=True, check=True
        )

        total_duration = get_duration(merged)

        # ── 3. Завантажуємо аудіо файли (якщо є) ────────────────────────
        has_narrator = bool(data.get('narrator'))
        has_music    = bool(data.get('music'))
        has_ambient  = bool(data.get('ambient'))

        if has_narrator:
            try:
                download(data['narrator'], narrator)
            except:
                has_narrator = False

        if has_music:
            try:
                download(data['music'], music)
            except:
                has_music = False

        if has_ambient:
            try:
                download(data['ambient'], ambient)
            except:
                has_ambient = False

        narrator_start = int(data.get('narrator_start', 6))

        # ── 4. Якщо немає жодного аудіо — просто повертаємо відео ───────
        if not any([has_narrator, has_music, has_ambient]):
            os.rename(merged, final)

        else:
            # ── 5. Будуємо FFmpeg команду з мікшуванням аудіо ───────────
            cmd = ["ffmpeg", "-y", "-i", merged]
            filter_parts = []
            mix_labels = []
            idx = 1

            # Музика: тихо (20%), весь час
            if has_music:
                cmd += ["-i", music]
                filter_parts.append(
                    f"[{idx}:a]aloop=loop=-1:size=2e+09,"
                    f"atrim=duration={total_duration},"
                    f"volume=0.2[mus]"
                )
                mix_labels.append("[mus]")
                idx += 1

            # Ambient: дуже тихо (10%), весь час
            if has_ambient:
                cmd += ["-i", ambient]
                filter_parts.append(
                    f"[{idx}:a]aloop=loop=-1:size=2e+09,"
                    f"atrim=duration={total_duration},"
                    f"volume=0.1[amb]"
                )
                mix_labels.append("[amb]")
                idx += 1

            # Narrator: повна гучність, починається з narrator_start секунди
            if has_narrator:
                cmd += ["-i", narrator]
                delay_ms = narrator_start * 1000
                filter_parts.append(
                    f"[{idx}:a]adelay={delay_ms}|{delay_ms},"
                    f"volume=1.0[nar]"
                )
                mix_labels.append("[nar]")
                idx += 1

            # Мікшуємо всі аудіо потоки разом
            n = len(mix_labels)
            mix_str = "".join(mix_labels)
            filter_parts.append(
                f"{mix_str}amix=inputs={n}:duration=first:"
                f"dropout_transition=2[aout]"
            )

            filter_complex = ";".join(filter_parts)

            cmd += [
                "-filter_complex", filter_complex,
                "-map", "0:v",
                "-map", "[aout]",
                "-c:v", "copy",
                "-c:a", "aac",
                "-shortest",
                final
            ]

            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                return jsonify({
                    "error": "FFmpeg audio mix failed",
                    "details": result.stderr[-500:]
                }), 500

        # ── 6. Повертаємо URL до готового відео ─────────────────────────
        base = os.environ.get("BASE_URL", "https://your-app.up.railway.app")
        return jsonify({
            "url": f"{base}/download/{uid}",
            "duration": total_duration,
            "job_id": uid
        })

    except requests.exceptions.RequestException as e:
        return jsonify({"error": f"Download failed: {str(e)}"}), 500
    except subprocess.CalledProcessError as e:
        return jsonify({"error": f"FFmpeg failed: {e.stderr}"}), 500
    except KeyError as e:
        return jsonify({"error": f"Missing field: {str(e)}"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    finally:
        # Очищаємо тимчасові файли
        for path in [v1, v2, v3, narrator, music, ambient, merged, list_f]:
            if os.path.exists(path):
                os.remove(path)


@app.route('/download/<uid>')
def download_file(uid):
    p = OUTPUT_DIR / f"{uid}.mp4"
    return send_file(str(p)) if p.exists() else ("Not found", 404)


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
