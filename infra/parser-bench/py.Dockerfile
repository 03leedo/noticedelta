# 파서 비교용 Python 파서 이미지. 실행은 네트워크 없이(run.sh 참고).
FROM python:3.12-slim
RUN pip install --no-cache-dir --root-user-action=ignore \
      hwp-hwpx-parser==1.0.0 olefile==0.47 pdfplumber==0.11.10
COPY bench_py.py /bench/bench_py.py
USER nobody
ENTRYPOINT ["python", "-I", "/bench/bench_py.py"]
