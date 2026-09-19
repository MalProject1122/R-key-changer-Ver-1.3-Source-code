# R-key changer 開発者向けメモ（Ver 1.3）

利用者向けの説明は [README.md](README.md) と `assets/guide.html` を参照してください。
このファイルは、コードを読む人・修正する人のためのメモです。

---

## 1. 構成

```
再生アプリ → CABLE Input（既定の出力を自動で切り替え）→ CABLE Output
  → 入力ストリーム → キュー → 出力コールバックで処理 → ヘッドホン / スピーカー
```

出力コールバック（`LiveMonitor._on_output`）の中の処理順:

```
入力 → キー変更（WSOLA） → EQ → LIVE SPACE → NaN/Inf チェック → 出力
```

| ファイル | 役割 |
|---|---|
| `main.py` | 起動、ログ設定、未処理例外のログ出力 |
| `config.py` | バージョン、パス（exe 版は exe と同じフォルダに設定を保存） |
| `audio/live_monitor.py` | 入出力ストリーム、`_RingPitchShifter`（WSOLA のリアルタイム版）、停止の検知 |
| `audio/time_stretch.py` | WSOLA 本体（numpy のみ） |
| `audio/equalizer.py` | 10バンド EQ（C の DLL を ctypes で呼ぶ） |
| `audio/live_space.py` | 音場（鏡像法の初期反射、FDN-8 残響、エコー、Mid/Side） |
| `audio/hrtf.py` | 球頭モデルの HRTF（バイノーラル） |
| `audio/default_device.py` | Windows の既定出力の読み書き（非公開 COM の `IPolicyConfig`） |
| `audio/app_settings.py` | `app_settings.json` の読み書きと値の検証 |
| `gui/live_monitor_window.py` | 画面全体（接続 / キー / 音響 / EQ / マイプリセット / YouTube 検索 / 📊） |
| `gui/sysinfo_monitor.py` | メモリ・CPU 使用率（C の DLL を ctypes で呼ぶ） |
| `gui/theme.py` | 色と自作ウィジェット（グラフ、メーター等） |
| `native/biquad_eq.c` | EQ 本体（RBJ peaking biquad × 10段、Q=1.0） |
| `native/sysinfo.c` | `GlobalMemoryStatusEx` / `GetSystemTimes` |

### スレッドの関係

- **GUI スレッド**: 画面操作、EQ 係数の更新、Live Space パラメータの差し替え、既定デバイスの切り替え
- **入力コールバック**: 受け取ったブロックをキューに積むだけ（上限64ブロック）
- **出力コールバック**: キー変更・EQ・音響の計算をすべてここで行う。例外が出ても無音を書いて `error` に理由を残す（ストリームは止めない）

Live Space のパラメータは frozen dataclass をまるごと差し替える方式で、ロックはありません。
EQ は ctypes が C の実行中に GIL を手放すため、C を呼ぶ区間だけを短いロックで直列化しています。

---

## 2. ビルド

### DLL（Cのソースを変更したときだけ）

MSVC 64bit が必要です（MinGW の gcc は 32bit のため不可）。
Visual Studio 2022 Build Tools の `vcvars64.bat` を読み込んでから、`native` フォルダで:

```bat
cl /utf-8 /LD /O2 /Fe:biquad_eq.dll biquad_eq.c
cl /utf-8 /LD /O2 /Fe:sysinfo.dll sysinfo.c psapi.lib
```

- **`/utf-8` は必須**です。付けないと日本語コメントを CP932 として読み（警告 C4819）、行が壊れることがあります
- 依存は `KERNEL32.dll` のみ（VC++ 再頒布パッケージ不要）
- `native/*.dll` はリポジトリに含めています

### exe

```bash
python -m PyInstaller "R-key changer.spec" --noconfirm
```

- `dist/R-key changer.exe` が作られます。exe は Releases に添付し、ソースのリポジトリには含めません
- onefile 形式のため、起動のたびに一時フォルダへ展開されます。重いライブラリ（scipy など）を足すと起動が遅くなるので注意してください

### 検証のコツ

- GUI を試すときは `audio.default_device.is_available` を `lambda: False` にモックすると、実機の既定デバイスが切り替わりません
- `native/test_biquad_c.py` で EQ の精度（表示dBと実測の誤差）と速度を確認できます

---

## 3. Ver 1.3 で直したもの

### 3-1. 設定の NaN / Infinity 対策

- **問題**: `json.loads` は `NaN` / `Infinity` を通常の数値として読むため、`eq_gains` の検証を通り抜けていた。EQ に入るとフィルタの状態が NaN になり、以後の音声がすべて NaN になる。マイプリセットの `eq_gains` は検証されておらず、要素数不足で `IndexError` になった。Live Space の NaN は `_clamp` を素通りして最大値に化けていた
- **修正**: 3段階で防ぐ
  - `app_settings.py`: `math.isfinite` で有限値のみ受け付け。不正なら全バンド 0dB
  - `equalizer.py`: `_sanitize_db` を追加。個数違い・型違い・NaN は 0dB
  - `biquad_eq.c`: 非有限値は更新を拒否（今の係数を保つ）、±24dB に丸める
  - `live_space.py`: 非有限値・bool は既定値へ。マイプリセットの `live_space_output_mode` / `live_space_params` も検証

### 3-2. 既定の出力デバイスを役割ごとに ID で戻す

- **問題**: 復元がデバイス名のみで、3つの役割（Console / Multimedia / Communications）をすべて同じデバイスに戻していた。同名の機器も区別できなかった
- **修正**:
  - 起動前に役割ごとのデバイスID（`IMMDevice.GetId()`）を取得し、`app_settings.json` の `original_output_ids` に保存
  - 戻すときは、CABLE を向いている役割だけを元の ID へ個別に戻す
  - 元の機器が無い役割はログを残してスキップし、従来の名前による復元（元の名前 → 選択中の出力）に切り替える
  - 強制終了時は次回起動時に保存済みの ID で復旧する（CABLE を向いている役割の ID は上書きしない）
  - `IMMDeviceEnumerator` に `GetDevice` を末尾に追加（既存メソッドの vtable 位置は不変）

### 3-3. EQ の係数更新と音声処理の競合（監査 M-5）

- **問題**: ctypes は C の実行中に GIL を手放すため、GUI の `eq_set_band_db` と音声側の `eq_process` が同時に走り、係数5個の書き換え途中を読む可能性があった
- **修正**: C を呼ぶ区間（係数更新・処理・破棄・グラフ計算）だけを `threading.Lock` で囲む。係数更新は1バンド数マイクロ秒

### 3-4. 挿し直したデバイスが一覧に出ない

- **問題**: PortAudio はデバイス一覧を初期化時に一度だけ作るため、「一覧を更新」を押しても挿し直した機器が出なかった
- **修正**: 停止中の「一覧を更新」で PortAudio を初期化し直す（`rescan_devices`）

### 3-5. NaN が出たときの安全策

- **修正**: 出力コールバックで処理結果に NaN / Inf が混ざったら、そのブロックを無音にし、EQ と音響の内部状態をリセットする（ログは1回だけ）

### 3-6. テスト結果

| 項目 | 結果 |
|---|---|
| 不正な設定（NaN / Infinity / -Infinity / null / 文字列 / bool / 要素数不足 / 要素数過多） | ✅ すべて既定値に戻る |
| 壊れた設定とマイプリセットでの GUI 起動・操作・終了 | ✅ 例外0件 |
| EQ の精度と速度 | ✅ 誤差 0.00dB、速度も変更前と同じ |
| GUI からの連続更新と音声処理の同時実行（3000ブロック） | ✅ NaN・例外0件 |
| コールバック時間（2048サンプル） | ✅ 中央値 約1.8ms（予算 42.7ms）、変更前と同じ |
| Live Space の全パラメータを最小／最大にして60秒（バイノーラルあり・なし） | ✅ 有限値、振幅 1.0 以下 |
| 入力キューの上限 | ✅ 64ブロックで頭打ち |
| 約21分ぶんの音声処理でのメモリ増加 | ✅ 0.01MB |
| 実機ストリームの開始・停止と一覧の再取得 | ✅ |
| 入力・出力の停止の検知（コールバックを止めて模擬） | ✅ |
| 音声ループ（CABLE を出力に選択）の拒否 | ✅ |
| 既定デバイスの役割ごとの復元（モックで6シナリオ） | ✅ |

**未実施**: 実機での USB 抜き差し、実際の `taskkill /F`、Windows 再起動後の復旧、exe の再ビルドと起動、30分以上の連続使用、48kHz 以外のサンプルレート、聴感の確認。

---

## 4. 改善が必要なもの

### 4-1. 動作・安全性

| 内容 | 理由と対応案 |
|---|---|
| ストリームが止まっても自動で再接続しない | 抜去後は「一覧を更新」→「▶ 開始」が必要。自動再接続を足す場合は、デバイスの再出現を監視する仕組みが要る |
| 抜去直後の `stream.stop()` / `close()` がドライバ次第で固まる可能性 | 実機でしか確認できない。固まる場合は別スレッドで閉じる等の対策が必要 |
| CABLE を意図的に既定にしている利用者も、終了時に元のデバイスへ戻される | 従来からの動作。必要なら「戻さない」設定を追加 |
| 強制終了後の復旧が、起動直後ではなく「停止」か「終了」のとき | 既存の設計どおり。起動直後に戻すと CABLE への切り替えと衝突するため |
| `GetId()` の文字列を `CoTaskMemFree` していない | 起動時と終了時に数十バイトずつ。実害は小さい |

### 4-2. 監査の残り項目

| ID | 内容 |
|---|---|
| M-4 | `Equalizer.process()` は入力配列を直接書き換える場合がある（docstring に明記済み。今の呼び出し元では無害） |
| L-1 | `atexit.unregister` をしていない |
| L-2 | 音声コールバックの中で `logger.exception` を呼んでいる |
| L-3 | 2つ目以降の別のエラーがログに残らない |
| L-5 | `requirements.txt` にビルド用の依存（PyInstaller など）が書かれていない |
| L-6 | `native/test_biquad_c.py` がリポジトリに残っている |

### 4-3. ドキュメント・その他

- `audio/live_monitor.py` の docstring に、元アプリ（Agastia）の `video.video_analysis` への言及が残っている（このリポジトリには存在しない）
- README に Windows の音量スライダー、ボーカル／楽器スライダー、YouTube 検索の説明が無い
- `説明書.md` に「Windows 専用」の記述がある（Linux 版を出すときに更新が必要）

### 4-4. 今後の候補（未着手）

- ナイトモード（コンプレッサー）、出力レベルメーター、スリープタイマー、A/B 比較
- 響きのプリセット追加（Live Space のパラメータを足すだけ）
- WSOLA の C 化は見送り（実測で予算の 0.3〜1.0%。ラズパイで重いと分かった場合のみ再検討）

---

## 5. Linux / Raspberry Pi 対応（未着手）

DSP と GUI の大部分はそのまま使えます。置き換えが必要なのは次の部分です。

| Windows 版 | Linux 版での置き換え案 |
|---|---|
| VB-CABLE | PipeWire / PulseAudio の null-sink と、その monitor（録音側） |
| `audio/default_device.py`（IPolicyConfig COM） | `wpctl` か `pactl set-default-sink` |
| Windows のデバイス音量（IAudioEndpointVolume） | `pactl set-sink-volume` |
| `native/sysinfo.c`（Win32） | `/proc/meminfo` と `/proc/stat` を Python で読む |
| `biquad_eq.dll` | 同じソースを gcc でビルドして `.so`（ARM 用）。読み込むファイル名の分岐も必要 |
| `winreg`（ブラウザ検出） | `xdg-open` や `chromium` |
| ホスト API の優先順位（WASAPI / MME / WDM-KS） | PipeWire / PulseAudio / ALSA |
| ログの保存先 `%LOCALAPPDATA%` | `~/.local/state` など |
| フォント BIZ UDGothic | Noto Sans CJK など |
| exe（PyInstaller） | ラズパイ上でビルドするか、ソースと起動スクリプトで配布 |

**移植前に決めること**: ラズパイの機種、OS と音声サーバー、曲をどこで再生するか（ラズパイ上か外部機器か）、出力先（USB / HDMI / Bluetooth）、画面の有無。

推奨構成（案）: Raspberry Pi 5（4GB 以上）＋ Raspberry Pi OS 64bit（Bookworm。標準の音声サーバーが PipeWire）＋ USB オーディオ。
Pi 5 にはイヤホン端子がありません。処理能力が間に合うかは実機で測る必要があります。
