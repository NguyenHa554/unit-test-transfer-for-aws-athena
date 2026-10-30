# Athena Unit Test Tool

Ứng dụng local: dán một model SQL dbt để tạo ba query kiểm tra trên **Athena Engine 3**. Mỗi query có Copy/Download; có nút tải cả ba query trong một file SQL. Không cần AWS credentials, không dùng AI/API bên ngoài.

## Khởi động trên Windows

Yêu cầu **Python 3.12+** trên PATH. Trong PowerShell:

```powershell
cd 'C:\nguyen\Projects\VIB2\unit test tool'
.\start.ps1
```

Mở **http://127.0.0.1:8002**. Giữ terminal đang chạy; nhấn Ctrl+C để dừng. `start.ps1` tạo `.venv` và cài ba dependencies từ `requirements.txt`; lần cài đầu cần truy cập Python package registry. Port khác: `.\start.ps1 -Port 8003`.

Nếu `.venv` đã cài đủ dependencies, có thể khởi động trực tiếp, không chạy pip:

```powershell
& '.\.venv\Scripts\python.exe' server.py
```

App độc lập với `transfer tool`: không import code tại runtime từ thư mục đó. Mặc định đọc repo nằm cạnh app: `..\km-etl-newcore`, với project ở `dbt/dbt_project.yml`. Có thể đổi đường dẫn trong Cấu hình nâng cao, hoặc dùng `server.py --repo 'D:\path\repo'`.

## Cách sử dụng

1. Dán toàn bộ model SQL, gồm `config()` nếu có. Chọn ngày ETL (mặc định **2026-07-01**).
2. Nếu model có `is_incremental()`, chọn Full refresh hoặc Incremental. `{{ this }}` đơn giản được resolve từ target đã xác định.
3. Chọn phạm vi target và tích ô xác nhận. Mặc định `SYM_RUN_DATE = ngày ETL`; chỉ dùng được nếu model có cột này. Có thể nhập một điều kiện WHERE khác hoặc chọn rõ Toàn bộ target. Scope chỉ lọc **target**; nguồn giữ nguyên SQL đã dán.
4. Nhấn **Generate Unit Tests**. Target và unique key được tìm từ config/metadata. Nếu thiếu, mở Cấu hình nâng cao để nhập target và khóa. Nhiều khóa phân cách bằng dấu phẩy.
5. Kiểm tra target, scope, bảng nguồn và SQL đã chuyển. Copy hoặc Download query để tự chạy trên Athena Engine 3.

Nếu chưa nhận diện được model, nhập **tên model** để đọc config kế thừa/YAML. SQL đã dán vẫn là nguồn chuyển đổi; app không thay bằng SQL trong file. Chỉ tự gắn tên model khi nội dung khớp chính xác một file hiện tại.

Với CL_INVOICE: khi metadata không khai báo khóa unique, nhập `INTERNAL_KEY` sau khi xác nhận đó là khóa cần kiểm tra. **Không dùng `DEBIT_NOTE_NO` riêng làm khóa unique.**

### Ba testcase

| ID | Kiểm tra | Kết quả mong đợi |
|---|---|---|
| UT001 | Count nguồn **sau tất cả transformation/joins/filters** so với target cùng scope | Count bằng nhau; nguồn rỗng trả `EMPTY_SOURCE`, không tự coi là PASS |
| UT002 | Duplicate và null theo khóa đã xác định | 0 dòng; mỗi dòng lỗi là một nhóm khóa với `row_count` và `issue_type` |
| UT003 | Source minus target và target minus source | 0 dòng; dùng `EXCEPT ALL` để giữ chênh lệch số lần xuất hiện |

UT003 dùng danh sách cột cụ thể, không tự bỏ cột hoặc ép kiểu tiền/ngày để che khác biệt. Dòng lệch có `ut_diff_direction`: `SOURCE_MINUS_TARGET` hoặc `TARGET_MINUS_SOURCE`. Nếu tên này đã có trong model, tên nhãn được thêm dấu `_` ở đầu.

Thông tin testcase hiển thị theo 12 trường của UT.xlsx: Testcase ID, Test case type, Job name, Source Table, Source Column, Target Table, Target Column, Test Parameter, Expected Results, Actual Result, Status, Time-frame. Actual Result để trống; Status luôn **Not Executed**. Chữ PASS trong CASE của query chỉ được tính khi bạn thực thi SQL, không phải kết luận của app.

## Resolve metadata và giới hạn

- Ngữ cảnh mặc định là **DEV**, đọc `env_bronze`, `env_landing`, `env_km_staging` từ project với `target.name = 'dev'`. Repo hiện tại resolve thành `dev_bronze`, `dev_lnd`, `dev_km_stg`. Biến JSON có thể override rõ ràng; luôn kiểm tra physical tables hiển thị.
- Dùng model SQL/YAML/config hiện tại, không dùng tên bảng UAT cũ từ manifest để thay cho config hiện tại. Model/source không tồn tại sẽ bị chặn; không đoán hậu tố hoặc schema.
- Khóa lấy từ `unique_key`, primary/unique constraints, `unique` tests và `unique_combination_of_columns`. Test unique có scope riêng không được tự coi là khóa toàn cục. Thiếu/nhiều ứng viên: yêu cầu chọn khóa; UT001/UT003 vẫn dùng được nếu các thông tin khác đầy đủ.
- Dùng Jinja sandbox và SQLGlot để render/parse/transpile Spark → Trino. Macro SQL thuần được mở rộng từ repo. Macro cần database/dbt runtime, macro chưa biết hoặc hàm chưa hỗ trợ sẽ bị chặn, không bị thay bằng `NULL`. Không chạy hooks, materialization hay lệnh dbt.
- `SELECT *` từ CTE/subquery được mở rộng khi biết đủ tên cột. Star từ bảng vật lý không có catalog: yêu cầu cung cấp **toàn bộ** tên output theo đúng thứ tự. Biểu thức cuối cần alias rõ ràng; tên output không được trùng.
- Một số khác biệt Spark/Athena còn phụ thuộc kiểu nguồn, ANSI mode, timezone và dữ liệu; cảnh báo đi kèm kết quả. Hash Spark như `xxhash64` chưa có chuyển đổi được xác minh sẽ bị chặn. `element_at(split(x, '-'), 1)` dùng trong CL_INVOICE được hỗ trợ riêng; không tự cho phép mọi biến thể `element_at`.
- Metadata chỉ giúp resolve SQL; app **không xác nhận bảng/cột tồn tại trong Glue**, không kiểm tra kiểu runtime và không chứng minh dữ liệu khớp Symbols.
- UT001 so sánh kết quả SELECT hiện tại với phạm vi target bạn xác nhận. Với incremental MERGE, lịch sử/SCD, post-hook hoặc materialization thay đổi tập dữ liệu, count không nhất thiết 1:1. Chọn scope phù hợp hoặc dùng SELECT thể hiện đúng tập cần kiểm tra; app không mô phỏng các thao tác ghi đó.
- Window không có thứ tự duy nhất, mapping trùng hoặc joins nhân dòng được giữ nguyên. Không thêm dedup, aggregate hoặc fallback để làm test khớp.

## Kiểm tra phát triển

```powershell
& '.\.venv\Scripts\python.exe' -m pip install -r requirements-dev.txt
& '.\.venv\Scripts\python.exe' -m unittest -v
```

DuckDB chỉ phục vụ kiểm tra SQL trên **dữ liệu mẫu trong RAM**, không dùng trong app. Nếu không cài nó, hai bài kiểm tra thực thi dữ liệu mẫu được đánh dấu skipped; các kiểm tra khác vẫn chạy. CL_INVOICE integration test đọc file repo nếu có và xác nhận chuyển đổi 53 cột về DEV; không thực thi trên Athena.

Các kiểm tra gồm thiếu khóa/target, khóa composite/null/trùng, CTE/star, JOIN nhân dòng, UNION ALL/window, ngày chạy/incremental, macro, source YAML, thiếu dependency, scope, EXCEPT ALL hai chiều, và API local. Đã kiểm tra UI bằng trình duyệt: dán model, sinh ba query, đổi tab, Copy và tải file gộp.

## API local

- `GET /api/health`: repo mặc định và số lượng metadata.
- `POST /api/generate`: `{sql, etl_date, variables, mode, repo, model_name, target, keys, output_columns, scope_mode, target_filter, scope_confirmed}`.
- `scope_mode`: `snapshot`, `custom`, `all`; `mode`: trống, `full_refresh`, `incremental`.
- Kết quả: `status` (`ready`, `needs_input`, `blocked`), SQL chuyển đổi, bảng resolve, output columns, scope, ứng viên khóa và ba `tests`. Mỗi test có `ready`, `issues`, `sql`, `metadata`; test chưa đủ thông tin không có SQL để copy.

Server chỉ bind `127.0.0.1`, từ chối Origin/Host khác và chỉ phục vụ các static files đã khai báo. Không lưu SQL người dùng vào file hoặc log, không gọi AWS/database, không sửa repo đầu vào. Không publish server ra LAN/Internet.

Tham khảo ngữ nghĩa: [Athena Engine 3 hỗ trợ EXCEPT ALL](https://docs.aws.amazon.com/athena/latest/ug/engine-versions-reference-0003.html), [Spark built-in functions](https://spark.apache.org/docs/latest/sql-ref-functions-builtin.html), [Trino array functions](https://trino.io/docs/current/functions/array.html).
