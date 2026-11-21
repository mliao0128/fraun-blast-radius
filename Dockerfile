FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90 AS upstream
ADD https://codeload.github.com/tobymao/sqlglot/tar.gz/eb11aec4be8cb2122afe713a4816662c3f0fb2fa /tmp/sqlglot.tar.gz
RUN mkdir /upstream && tar -xzf /tmp/sqlglot.tar.gz -C /upstream --strip-components=1

FROM python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90
WORKDIR /app
COPY replay-requirements.txt ./
RUN pip install --no-cache-dir -r replay-requirements.txt
COPY --from=upstream /upstream /upstream
RUN python -m compileall -q -j 4 --invalidation-mode checked-hash /upstream/sqlglot /upstream/tests
COPY *.py *.json.gz ./
ENV PYTHONDONTWRITEBYTECODE=1 OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 SKIP_INTEGRATION=0
CMD ["python","/app/run.py"]

