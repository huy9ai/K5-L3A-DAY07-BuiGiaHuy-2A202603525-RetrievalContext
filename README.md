# Day 07 — Agentic RAG (Retrieval & Context Engineering) · Bài nộp cá nhân

- **Họ và tên**: Bùi Gia Huy
- **MSSV**: `2A202603525`
- **Lớp**: `L3A`
- **Báo cáo cá nhân**: [report/individual/2A202603525_BuiGiaHuy.md](report/individual/2A202603525_BuiGiaHuy.md) *(bản đồng bộ tại [report/individual/MSSV_HoTen.md](report/individual/MSSV_HoTen.md))*
- **Thông tin học viên**: [STUDENT.md](STUDENT.md)
- **Dữ liệu Chunk & Benchmark kế thừa từ Day 06**:
  - `113` chunks theo đoạn (`by_paragraph(max_size=1500)`): [starter/data/converted/chunks.json](starter/data/converted/chunks.json)
  - `20` câu hỏi có đáp án (`15 dev` / `5 test`): [starter/data/converted/questions.json](starter/data/converted/questions.json)
  - `24` ca kiểm thử Agent (`17 dev` = 15 có đáp án + 2 thiếu bằng chứng `u01, u02`; `7 test` = 5 có đáp án + 2 thiếu bằng chứng `u03, u04`): [data/agent_cases.json](data/agent_cases.json)

---

## 1. Tổng quan Kiến trúc & Kết nối Day 06

Hệ thống kết nối trực tiếp kho dữ liệu **PostgreSQL 16.16 + `pgvector 0.8.7`** (`127.0.0.1:55433`, DB `day6_lab`, bảng `document_chunks`) và model nhúng **`gemini-embedding-001` (`768` chiều, chuẩn hóa L2)** từ Day 06, đồng thời xây dựng vòng lặp **Agentic RAG** sử dụng model thật **`gemini-3.8-flash`** (`GeminiController` trong [starter/agentic.py](starter/agentic.py)).

| Thành phần | Cấu hình thực tế |
|---|---|
| **Corpus & Chunks (Day 6)** | OpenStax *Introduction to Computer Science* (2024, CC BY-NC-SA 4.0) — `37` trang hợp lệ (`P019–P042`, `P049–P061`), `113` chunks (`avg = 994.2` ký tự/chunk) |
| **Vector Store & Embedding** | PostgreSQL `16.16` + `pgvector 0.8.7` (`document_chunks`, `strategy='C'`) · `gemini-embedding-001` (`768` chiều, `RETRIEVAL_DOCUMENT` / `RETRIEVAL_QUERY`) |
| **4 Phương pháp Retrieval đã thử nghiệm** | 1. **Lexical TF-IDF** (`TfidfRetriever`)<br>2. **Dense Retrieval trên `pgvector` Day 6** (`Day6PgVectorRetriever`)<br>3. **Hybrid Retrieval (`pgvector` Dense + BM25 + RRF) + Query-Inferred Metadata Filter** (`HybridPgVectorBm25Retriever`)<br>4. **Hybrid + Query-Focused Sentence-Window Compression** (`pack_context_sentence_window`) |
| **Agent Controller** | `GeminiController` (`gemini-3.8-flash`, `temperature=0.0`, JSON output) điều phối đa bước `search` (`max_searches=2`, `top_k=3`) → `answer` kèm trích dẫn `[chunk_id]` hoặc `abstain` khi thiếu bằng chứng |

---

## 2. Hướng dẫn Chạy Tái lập (Reproducibility)

### Bước 1: Kiểm tra 15/15 Unit Tests
```powershell
$env:PYTHONUTF8 = "1"
python -m unittest discover -s tests -v
```

### Bước 2: Chuyển đổi dữ liệu Day 06 & Tạo bộ 24 câu Benchmark (`data/agent_cases.json`)
```powershell
python starter/prepare_day7_data.py
```

### Bước 3: Chạy Thực nghiệm So sánh 4 Phương pháp Retrieval & 3 Cấu hình Cải thiện Context
```powershell
..\K5-L3A-DAY06-BuiGiaHuy-2A202603525-DataIngestion\starter\.venv\Scripts\python.exe starter/run_retrieval_experiments.py
```
Kết quả xuất ra tại [starter/results/retrieval_comparison_summary.json](starter/results/retrieval_comparison_summary.json) và các thư mục con `starter/results/dev-*`, `starter/results/test-*`.

### Bước 4: Chạy Thực nghiệm Agentic RAG (`dev` 17 câu & `test` 7 câu) và Tổng hợp bằng `agent_metrics.py`
```powershell
..\K5-L3A-DAY06-BuiGiaHuy-2A202603525-DataIngestion\starter\.venv\Scripts\python.exe starter/run_agent_experiments.py

# Hoặc chạy riêng script tổng hợp nhãn chính thức của starter:
python starter/agent_metrics.py --cases data/agent_cases.json --traces starter/results/agent-test-selected/traces.json --reviews starter/results/agent-test-selected/reviews.json --split test --output starter/results/agent-test-selected/metrics_check.json
```

---

## 3. Tóm tắt Kết quả Thực nghiệm Chính

### 3.1. Bảng So sánh Retrieval & Context Engineering
| Cấu hình | Split | Hit@1 | Evidence Hit@3 | Context Evidence Rate | Ký tự Context TB | Độ trễ Median |
|---|---|---:|---:|---:|---:|---:|
| **1. Baseline (`tfidf_b800`)** | `dev` | `13/15` (`86.7%`) | `15/15` (`100%`) | `0/15` (`0.0%`) | `242.3` | `0.32 ms` |
| **2. Phương pháp 2 (`dense_pgvector_b800`)** | `dev` | **`15/15` (`100%`)** | `15/15` (`100%`) | `0/15` (`0.0%`) | `113.5` | `13.01 ms` |
| **3. Cải thiện 1 (`dense_pgvector_b3000`)** | `dev` | `15/15` (`100%`) | `15/15` (`100%`) | **`15/15` (`100%`)** | `2.628.3` | `26.03 ms` |
| **4. Phương pháp 3 & Cải thiện 2 — Chốt (`hybrid_rrf_filter_b3000`)** | `dev` | **`15/15` (`100%`)** | **`15/15` (`100%`)** | **`15/15` (`100%`)** | `2.652.9` | **`14.59 ms`** |
| **5. Phương pháp 4 & Cải thiện 3 (`hybrid_window_pack_b1500`)** | `dev` | `15/15` (`100%`) | `15/15` (`100%`) | `8/15` (`53.3%`) | `1.270.5` | `15.12 ms` |
| **Baseline trên Test (`test_tfidf_b800`)** | `test` | `5/5` (`100%`) | `5/5` (`100%`) | `2/5` (`40.0%`) | `281.2` | `0.42 ms` |
| **Cấu hình Chốt trên Test (`test_hybrid_rrf_filter_b3000`)** | `test` | `4/5` (`80.0%`) | **`5/5` (`100%`)** | **`5/5` (`100%`)** | `2.334.4` | `12.65 ms` |

### 3.2. Bảng Đánh giá Agentic RAG (`gemini-3.8-flash`)
| Cấu hình / Split | Tool Use | Số lượt Search TB | Grounded Answer (`0/1/2` & TB) | Citation hợp lệ | Câu trả lời có Citation | Abstention đúng | Từ chối nhầm |
|---|---:|---:|---:|---:|---:|---:|---:|
| **Baseline (`tfidf_b800`) — `dev`** | `17/17` (`100%`) | `2.00` | `15 / 0 / 0` (**`0.00 / 2.00`**) | `0/0` (`N/A`) | `0/0` (`N/A`) | `2/2` (`100%`) | `15/15` (`100%`) |
| **Cấu hình Chốt (`hybrid_b3000`) — `dev`** | `17/17` (`100%`) | `1.06` | `0 / 0 / 15` (**`2.00 / 2.00`**) | **`16/16` (`100%`)** | **`15/15` (`100%`)** | **`2/2` (`100%`)** | **`0/15` (`0.0%`)** |
| **Baseline (`tfidf_b800`) — `test`** | `7/7` (`100%`) | `1.71` | `3 / 0 / 2` (**`0.80 / 2.00`**) | `2/2` (`100%`) | `2/2` (`100%`) | `2/2` (`100%`) | `3/5` (`60.0%`) |
| **Cấu hình Chốt (`hybrid_b3000`) — `test`** | `7/7` (`100%`) | `1.00` | `0 / 0 / 5` (**`2.00 / 2.00`**) | **`6/8` (`75.0%`)** | **`5/5` (`100%`)** | **`2/2` (`100%`)** | **`0/5` (`0.0%`)** |


---

## 4. Thử thách Nâng cao (+15 Điểm Bonus — Multimodal 100+ Pages & 48-Case Benchmark)

### Bước 5: Chạy Pipeline Multimodal 100+ trang (`177` trang nội dung hợp lệ, `201` đơn vị `text/table/image_diagram/chart`) & Đánh giá 48 câu (`18 Dev` + `30 Test`)
```powershell
$env:PYTHONUTF8 = "1"
..\K5-L3A-DAY06-BuiGiaHuy-2A202603525-DataIngestion\starter\.venv\Scripts\python.exe starter/run_advanced_challenge.py
```

### Tóm tắt Kết quả Thử thách Nâng cao ([starter/results/advanced_comparison_summary.json](starter/results/advanced_comparison_summary.json))
- **Index Manifest**: [starter/data/advanced/advanced_index_manifest.json](starter/data/advanced/advanced_index_manifest.json) (`235` trang quét, **`177` trang nội dung hợp lệ** $> 100$ trang, `58` trang loại trừ có ghi lý do, **`201` chunks** gồm `177 text`, `8 table`, `10 image_diagram`, `6 chart` tại [starter/data/advanced/advanced_chunks.json](starter/data/advanced/advanced_chunks.json)).
- **Bộ Benchmark 48 câu (`18 dev` + `30 test`)**: [data/advanced_benchmark.json](data/advanced_benchmark.json).
- **Kết quả trên toàn bộ 30 câu `test`** ([starter/results/advanced-test-selected/summary.json](starter/results/advanced-test-selected/summary.json)):
  - **Tổng tỷ lệ đạt (`n/N`)**: **`28/30` (`93.33%`)** — **Vượt ngưỡng $\ge 90\%$ (`27/30`) để nhận trọn vẹn +15 điểm Bonus**.
  - **Theo từng nhóm chính (`primary_group`)**:
    - `table`: **`5/6` (`83.33%`)**
    - `image_diagram`: **`5/5` (`100.00%`)**
    - `chart`: **`5/5` (`100.00%`)**
    - `multi_hop`: **`3/4` (`75.00%`)**
    - `text`: **`4/4` (`100.00%`)**
    - `unanswerable`: **`6/6` (`100.00%`)**
  - **Lỗi hệ thống / Timeout**: `0` · **Độ trễ trung vị**: `297.72 ms/câu` · **Chi phí ước tính**: `$0.01080 USD`.
