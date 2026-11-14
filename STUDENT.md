# Thông tin học viên — Day 07 · Agentic RAG (Retrieval & Context Engineering)

- Họ và tên: Bùi Gia Huy
- MSSV: 2A202603525
- Lớp: L3A
- Chủ đề và corpus (kế thừa từ Day 06): Khoa học Máy tính — Giáo trình *Introduction to Computer Science* (OpenStax, Rice University, 2024, giấy phép mở CC BY-NC-SA 4.0, ISBN-13: `978-1-961584-58-7`). Gồm **37 trang nội dung hợp lệ** (`112.238` ký tự sau chuẩn hóa) được chia thành **113 chunks theo đoạn (`by_paragraph(max_size=1500)`)**, lưu trên **PostgreSQL 16.16 + pgvector 0.8.7** (`day6_lab`, bảng `document_chunks`) với vector nhúng **`gemini-embedding-001` (`768` chiều)**.
- Agent Model: Google Gemini `gemini-3.8-flash` (điều phối đa bước `search` / `answer` kèm trích dẫn `[chunk_id]` và từ chối `abstain` khi thiếu bằng chứng).
- Repo Day 06: https://github.com/huy9ai/K5-L3A-DAY06-BuiGiaHuy-2A202603525-DataIngestion
- Repo bài nộp Day 07: https://github.com/huy9ai/K5-L3A-DAY07-BuiGiaHuy-2A202603525-RetrievalContext

