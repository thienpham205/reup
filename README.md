# Douyin Dub – Phụ đề + lồng tiếng Việt cho video Douyin

Dán link Douyin vào, tool sẽ tự động:

1. **Tải video** không watermark
2. **Nghe tiếng Trung** (faster-whisper, chạy offline trên máy, lấy thời gian theo từng chữ)
3. **Dịch sang tiếng Việt**: bằng AI (Gemini/Claude/OpenAI/DeepSeek) nếu có API key, không thì Google Dịch
4. **Lồng tiếng Việt** (giọng AI Microsoft Edge, căn khớp thời gian từng câu; tiếng gốc chỉ nhỏ đi khi giọng Việt đang nói)
5. **Giữ nguyên hình gốc** (kể cả phụ đề Trung in sẵn), chỉ thay tiếng bằng giọng Việt. Phụ đề Việt xuất ra file `.srt`;
   muốn ghép cứng vào video thì bật tuỳ chọn (khi đó tool tự làm mờ phụ đề Trung rồi đặt phụ đề Việt đè lên)

## Cài đặt (một lần)

```bash
pip install -r requirements.txt
python -m playwright install chromium
```

Cần có `ffmpeg` (máy này đã có sẵn). Lần chạy đầu sẽ tải model nhận dạng giọng nói (~1,5 GB với model `medium`).

Douyin chặn tải video tự động, nên tool mở trang video bằng một trình duyệt Chromium ẩn (giống người xem thật) để lấy link video gốc không watermark.

## Bật dịch bằng AI (nên làm)

Google Dịch dịch từng câu riêng lẻ nên hay sai ngữ cảnh, xưng hô cứng, câu dài hơn lời gốc (giọng lồng phải đọc vội). Dịch bằng AI thì:

- hiểu cả đoạn hội thoại, tự sửa chỗ máy nghe nhầm chữ đồng âm
- lập bảng nhân vật + xưng hô cố định cho từng cặp (VD cháu ↔ cô), dịch nhất quán cả video
- đoán ai nói câu nào để lồng mỗi nhân vật một giọng
- tên nhân vật đọc Hán-Việt (明哥 → anh Minh)
- viết câu vừa đủ thời gian nói; câu nào vẫn dài sẽ được rút gọn lại

Cách bật: trên web, mục **Dịch bằng AI** → chọn nhà cung cấp → dán API key → **Lưu** → **Kiểm tra**.
Gemini có gói miễn phí: lấy key ở https://aistudio.google.com/app/apikey

Key được lưu trong `config.json` cạnh tool (đã nằm trong `.gitignore`). Dùng dòng lệnh thì có thể đặt biến môi trường `DUB_AI_PROVIDER` (`gemini`/`claude`/`openai`/`deepseek`) và `DUB_AI_KEY`.

## Cách dùng

### Giao diện web (dễ nhất)

Nhấp đúp `mo-web.bat`. Trình duyệt sẽ tự mở trang http://localhost:5577:

- Dán link Douyin (hoặc tải file video lên), chọn giọng nam/nữ, bấm **Bắt đầu dịch**
- Xem tiến độ từng bước: tải video → nghe tiếng Trung → dịch → lồng tiếng → xuất video
- Xong thì xem video ngay trên web, tải video/phụ đề về, xem lời thoại song ngữ (bấm vào câu để tua tới đó)
- Có lịch sử các video đã làm; gửi nhiều link cùng lúc thì tool sẽ xếp hàng xử lý lần lượt

Web chỉ chạy trên máy bạn; đóng cửa sổ đen là tắt web. Kết quả lưu trong `output/jobs/` (hoặc thư mục đã chọn ở
Cài đặt → Lưu trữ).

### Dòng lệnh

Nhấp đúp `dich.bat`, dán link và Enter. Hoặc:

```bash
python douyin_dub.py "https://v.douyin.com/xxxxxxx/"
```

Có thể dán nguyên đoạn chia sẻ của Douyin (kèm chữ Trung), tool tự lấy link ra.

Kết quả nằm trong thư mục `output/`:

| File | Nội dung |
|---|---|
| `<tên>.vi.mp4` | Video đã lồng tiếng + phụ đề Việt |
| `<tên>.goc.mp4` | Video gốc |
| `<tên>.vi.srt` / `<tên>.zh.srt` | Phụ đề Việt / Trung |

## Bộ đăng bài: ảnh bìa, tiêu đề, hashtag

Video xử lý xong, tool tự tạo luôn (tắt được bằng công tắc **Tự tạo ảnh bìa, tiêu đề, hashtag**):

- **Ảnh bìa** (`cover.jpg`): lấy 6 khung hình trong video, chọn khung nét nhất rồi in chữ tiêu đề lên.
  Trên web đổi được khung hình, kiểu chữ (chữ đậm / hộp màu / không chữ) và nội dung chữ.
- **Tiêu đề, mô tả, hashtag**: có key AI thì AI đọc lời thoại và viết 3 tiêu đề để chọn; không có key thì
  tạo theo mẫu đơn giản. Sửa trực tiếp trên web, bấm **Sao chép tất cả** để dán lúc đăng.

Xem ở thẻ **Bộ đăng bài** cạnh video. Cần thêm thư viện `Pillow` (đã có trong `requirements.txt`).

## Liên kết tài khoản Facebook, YouTube, TikTok

Trang **Tài khoản** trên web dùng đăng nhập chính thức (OAuth) của từng nền tảng; tool không thấy mật khẩu.
Mỗi nền tảng cần một ứng dụng do bạn tạo trên trang nhà phát triển (miễn phí), các bước ghi ngay trên web:

| Nền tảng | Tạo ứng dụng ở | Liên kết được |
|---|---|---|
| YouTube | Google Cloud Console (bật YouTube Data API v3) | Kênh YouTube |
| Facebook | Meta for Developers | Trang (Page), không phải trang cá nhân |
| TikTok | TikTok for Developers (Login Kit + Content Posting API) | Tài khoản TikTok |

Trên web: xem danh sách tài khoản, số người đăng ký / thích, kiểm tra kết nối, gỡ liên kết.
Mã truy cập lưu trong `accounts.json` cạnh tool (đã nằm trong `.gitignore`).

### Đăng video

Ở thẻ **Bộ đăng bài** của video đã xong: tick các tài khoản, chọn **Riêng tư** hoặc **Công khai**, bấm **Đăng video**.
Tool đăng video tiếng Việt kèm tiêu đề, mô tả, hashtag và ảnh bìa, hiện tiến độ và link video sau khi xong.

- YouTube: ảnh bìa riêng chỉ đặt được khi kênh đã xác minh số điện thoại. Dự án Google Cloud chưa được Google
  kiểm duyệt thì video tải qua API bị khoá ở chế độ riêng tư.
- Facebook: "Riêng tư" nghĩa là video chưa công bố trên Trang.
- TikTok: ứng dụng chưa được TikTok duyệt chỉ đăng được ở chế độ riêng tư; ảnh bìa là khung hình đã chọn.

**Hẹn giờ đăng:** bật "Hẹn giờ đăng", chọn ngày giờ (tối đa 60 ngày tới). Danh sách mọi bài đang hẹn nằm ở trang
**Tài khoản → Lịch đăng**, huỷ được từng bài. Tool phải đang chạy lúc tới giờ; nếu lúc đó tắt máy, bài sẽ được đăng
ngay khi bạn mở lại tool (và ghi chú "đăng trễ").

## Xử lý hàng loạt

Dán nhiều link vào ô link (mỗi dòng một link, hoặc dán liền nhiều đoạn chia sẻ). Mỗi link thành một video trong hàng
đợi, xử lý lần lượt với cùng tuỳ chọn. Tối đa 50 link mỗi lần.

## Bản video dọc 9:16 và logo / chữ ký

- **Bản video dọc** (Shorts / Reels / TikTok): ngoài video gốc, tool xuất thêm file `*.doc.mp4` 1080×1920.
  *Nền mờ* giữ trọn hình (trên/dưới là nền mờ của chính video), *Cắt giữa* phóng to cho kín khung và cắt hai bên.
  Video vốn đã dọc thì chỉ đưa về đúng cỡ. Khi đăng, chọn "Bản dọc 9:16" hoặc "Bản gốc".
- **Logo / chữ ký**: đặt ở trang **Cài đặt** (ảnh logo PNG, chữ như `@tenkenh`, góc, cỡ, độ đậm; có ảnh xem trước),
  rồi bật "Gắn logo / chữ ký" khi tạo video. Logo gắn vào cả bản gốc lẫn bản dọc; ở bản dọc logo tránh mép trên/dưới
  nơi TikTok, Reels đặt nút bấm.

## Dịch offline bằng Hunyuan-MT (mã nguồn mở)

Mục **Bộ dịch** → **Hunyuan-MT (offline)**: dùng [Hunyuan-MT-7B](https://github.com/Tencent-Hunyuan/Hunyuan-MT) của
Tencent, model dịch mã nguồn mở đứng đầu 30/31 cặp ngôn ngữ ở cuộc thi dịch máy WMT25, chạy ngay trên máy bằng
[llama.cpp](https://github.com/ggml-org/llama.cpp) (CPU), không cần mạng hay API key. Lần đầu tool tự tải bộ chạy
llama.cpp (~25 MB) và model nén Q4_K_M (~4,6 GB, bản của mradermacher trên Hugging Face) vào thư mục models
(tải dở thì lần sau tải tiếp). Cần khoảng 6 GB RAM trống.

- Dịch sát nghĩa, tự nhiên hơn Google Dịch; dịch theo cụm vài câu để giữ ngữ cảnh xưng hô.
- Không phân vai nhân vật như dịch bằng AI có key (giọng đoán theo cao độ giọng gốc), không viết tiêu đề.
- **Tự động** = AI nếu có API key, không thì Google. Có key Gemini (miễn phí) thì AI vẫn là lựa chọn tốt nhất.

## Thêm giọng: Edge nhiều giọng

Bộ đọc **Edge nhiều giọng**: vai chính dùng 2 giọng tiếng Việt của Edge (HoaiMy, NamMinh), mỗi vai phụ một giọng
Edge "đa ngôn ngữ" khác cũng đọc được tiếng Việt: nữ Emma, Seraphina, Ava, Thalita, Vivienne; nam Rémy, Florian,
Hyunsu, Brian, Giuseppe, William. Tất cả miễn phí, đã cho máy nghe lại để chấm độ rõ (đa số nghe ra đúng 95–100%
chữ); vai phụ đôi khi hơi lơ lớ. Tổng cộng tool có: 2 + 11 giọng Edge, 2 giọng nữ + 65 người đọc Piper (offline),
Gemini, ElevenLabs.

Khi máy chủ Edge lỗi mãi (hay gặp lỗi 503 tạm thời), câu đó tự đọc bằng Piper chạy trên máy thay vì dừng cả video.
Video lỗi ở bước lồng tiếng có nút **Lồng lại (giữ bản dịch)**: không phải tải / nghe / dịch lại.

## Sửa lời thoại rồi lồng lại

Ở thẻ **Lời thoại** của video đã xong, bấm **Sửa lời thoại / người nói**: sửa câu tiếng Việt, đổi người nói của
từng câu (giọng đổi theo nhân vật), bấm ▶ để nghe đoạn đó. **Lưu & lồng lại** chỉ đọc lại giọng và xuất lại video
(cả bản dọc, logo), không tải / nghe / dịch lại, nên nhanh hơn nhiều. Câu bạn đã sửa không bị AI rút gọn nữa; câu dài
thì đọc nhanh hơn. Lời thoại lưu trong `segs.json` của từng video (video làm bằng bản cũ thì không có).

## Tách giọng gốc khỏi nhạc nền

Bật **Tách hẳn giọng Trung, giữ nhạc nền**: tool dùng model mã nguồn mở MDX-Net *Kim_Vocal_2* của
[Ultimate Vocal Remover](https://github.com/Anjok07/ultimatevocalremovergui) (~66 MB, tự tải lần đầu, chạy bằng
`onnxruntime` trên CPU) để bỏ giọng nói gốc, giữ nguyên nhạc và tiếng động. Mỗi phút video chậm thêm khoảng 45 giây –
1 phút. Nhạc nền đã tách lưu thành `nhacnen.m4a`, lồng lại không phải tách lần nữa.

## Phụ đề kiểu TikTok

Khi bật bản dọc, bật thêm **Phụ đề kiểu TikTok trên bản dọc**: chữ to, đậm, từng từ chuyển vàng theo giọng đọc.
Với bản *Nền mờ*, chữ nằm ngay dưới khung video (không đè phụ đề Trung in sẵn); bản *Cắt giữa* / video vốn dọc thì
chữ ở khoảng 2/3 chiều cao, tránh vùng nút bấm của TikTok / Reels. Thời gian từng từ là ước lượng theo độ dài chữ.

## Tìm video theo từ khoá (Douyin, Bilibili, YouTube)

Trang **Tìm video**: chọn nền tảng, gõ từ khoá (tiếng Trung cho kết quả tốt nhất), lọc theo sắp xếp (phù hợp / nhiều
thích / mới nhất), thời gian đăng, độ dài, lượt thích tối thiểu. Kết quả hiện ảnh bìa, tiêu đề đã dịch sang tiếng
Việt, lượt thích (Bilibili / YouTube: lượt xem), tác giả; video đã dịch rồi có nhãn "Đã có". Tích chọn rồi bấm
**Dịch N video** để đưa vào hàng đợi với tuỳ chọn đang chọn ở trang Lồng tiếng.

- **Douyin** chỉ cho tìm kiếm khi đã đăng nhập: bấm **Đăng nhập Douyin**, một cửa sổ trình duyệt của tool mở ra,
  bạn tự quét mã QR bằng app Douyin rồi đóng cửa sổ. Phiên đăng nhập lưu trong hồ sơ trình duyệt của tool; tool không
  thấy mật khẩu. Bảng **xu hướng Douyin** (bấm để tìm) không cần đăng nhập.
- **Bilibili** (API tìm kiếm công khai) và **YouTube** (yt-dlp) không cần đăng nhập. Video Bilibili / YouTube tải
  bằng yt-dlp, ghép hình + tiếng, tối đa 1080p.
- **⟳ Tự động theo từ khoá**: tool định kỳ tìm lại từ khoá với cùng bộ lọc, mỗi lần dịch tối đa 3 video mới (xem và
  tạm dừng ở trang Theo dõi kênh).

## Theo dõi kênh (tự dịch video mới)

Trang **Theo dõi kênh**: dán link trang tác giả Douyin (`douyin.com/user/...`) hoặc kênh YouTube / TikTok, chọn
kiểm tra mỗi bao nhiêu giờ. Có video mới thì tool tự tạo việc dịch với **tuỳ chọn đang chọn ở trang Lồng tiếng**.
Lần đầu có thể lấy luôn 1–5 video mới nhất. Bật **Tự đăng khi làm xong** để đăng lên các tài khoản đã liên kết, có thể
giãn cách (ví dụ mỗi ngày 1 video, các video sau tự xếp lịch). Mỗi lần kiểm tra lấy tối đa 5 video. Danh sách kênh lưu
trong `watches.json`. Tool phải đang chạy thì mới kiểm tra được.

## Báo qua Telegram

Cài đặt → **Báo qua Telegram**: tạo bot bằng [@BotFather](https://t.me/BotFather) (`/newbot`), bấm Start trong bot
vừa tạo, dán token vào web rồi bấm **Kết nối** (tool tự tìm cuộc trò chuyện của bạn). Chọn báo khi nào: video xong,
video lỗi, đăng bài xong / lỗi, kênh theo dõi có video mới.

## Lưu trữ

Cài đặt → **Lưu trữ** cho biết dữ liệu nằm ở đâu, chiếm bao nhiêu, ổ còn trống bao nhiêu.

- **Tự xoá video cũ** sau 3–90 ngày (video còn bài hẹn giờ thì giữ), hoặc bấm **Dọn ngay**.
- **Chuyển dữ liệu sang thư mục khác**: chuyển video, model nhận dạng giọng nói (~1,5–2 GB), giọng Piper, model tách
  giọng và trình duyệt ẩn sang ổ khác. Đường dẫn ghi vào `config.json` (`data_dir`). Máy này đã chuyển sang
  `D:\LongTiengViet-data`.

## Giọng đọc mã nguồn mở Piper

Ngoài Edge, Gemini, ElevenLabs, tool có thêm [Piper](https://github.com/OHF-Voice/piper1-gpl) — bộ đọc mã nguồn mở
chạy ngay trên máy, không cần mạng, không giới hạn. Giọng tiếng Việt lấy từ kho mở
[rhasspy/piper-voices](https://huggingface.co/rhasspy/piper-voices/tree/main/vi/vi_VN) (~150 MB, tự tải lần đầu
vào thư mục `voices/`):

| Giọng | Mô tả |
|---|---|
| VAIS1000 | 1 giọng nữ thu âm studio, rõ nhất |
| 25hours | 1 giọng nữ |
| VIVOS | 65 người đọc nam và nữ (16 kHz, rõ kém hơn) |

Mỗi nhân vật nhận một giọng khác nhau; giọng đã được xếp theo độ rõ (cho máy nghe lại và chấm điểm). Trẻ con dùng
giọng nữ cao và nâng cao độ, người già hạ cao độ, đọc chậm hơn.

- **Piper**: mọi nhân vật đọc bằng Piper.
- **Edge + Piper**: vai chính nam/nữ đọc bằng Edge (rõ nhất), các vai phụ mỗi người một giọng Piper. Hợp với phim
  nhiều nhân vật, vì Edge chỉ có 2 giọng tiếng Việt.

Giọng Piper nghe rõ kém Edge một chút (một số model cũ không có dấu thanh trong bảng âm vị). Muốn đổi thứ tự giọng,
tạo file `voices/pool.json` dạng `{"female": ["vais1000", "vivos:27"], "male": ["vivos:45"], "child": ["vivos:0"]}`.

## Tuỳ chọn

| Tuỳ chọn | Ý nghĩa |
|---|---|
| `--voice nam` / `--voice nu` | Một giọng nam / nữ cho cả video. Mặc định `auto`: AI xác định nhân vật, mỗi nhân vật một giọng (nam, nữ, trẻ con = giọng nữ nâng cao độ, người già = hạ cao độ) |
| `--model small` | Nghe nhanh hơn ~2 lần nhưng hay nhầm chữ đồng âm (mặc định `medium`) |
| `--no-ai` | Không dùng AI, dịch bằng Google |
| `--subs` | Ghép cứng phụ đề Việt vào video (mặc định không ghép, giữ nguyên hình gốc) |
| `--cover-subs off` | Khi dùng `--subs`: không làm mờ phụ đề Trung in sẵn |
| `--no-dub --subs` | Chỉ thêm phụ đề Việt, giữ tiếng gốc |
| `--bg-volume 0.3` | Tiếng gốc to hơn khi đang lồng tiếng (0 = tắt hẳn) |
| `--max-speed 1.3` | Giới hạn tốc độ đọc khi câu dịch dài |
| `--font-size 16` | Chữ phụ đề to hơn |
| `-o D:\Videos` | Đổi thư mục lưu |
| `--cookies-from-browser chrome` | Dùng khi Douyin chặn tải |
| `--separate` | Tách hẳn giọng gốc khỏi nhạc nền |
| `--translator hunyuan` | Dịch offline bằng Hunyuan-MT-7B (`google` = luôn dùng Google Dịch) |
| `--tts edgeplus` | Mỗi vai phụ một giọng Edge đa ngôn ngữ riêng |
| `--redub -o <thư mục video>` | Lồng lại từ `segs.json` đã sửa (bỏ qua tải / nghe / dịch) |

Cũng dùng được với file video có sẵn: `python douyin_dub.py video.mp4`, và các link TikTok/YouTube/Bilibili.

## Lưu ý

- Máy không có GPU nên chạy bằng CPU: video 1 phút 46 giây mất khoảng 6 phút với model `medium`.
- Giọng đọc Microsoft và Google Dịch đôi khi tạm từ chối yêu cầu; tool tự thử lại.
- Tiếng Việt của Microsoft chỉ có 2 giọng (1 nam, 1 nữ): giọng trẻ con và nhiều nhân vật cùng giới được tạo bằng cách chỉnh cao độ.
- AI đoán người nói theo nội dung lời thoại nên đôi khi nhầm (câu của nhân vật này lồng giọng nhân vật khác).
- Video chỉ có nhạc, không có lời thoại thì không có gì để dịch.
