# Hello World Java API

Simple Java HTTP API demonstrating performance testing pipeline integration.
No frameworks — uses Java's built-in `com.sun.net.httpserver`.

---

## Project Structure

```
hello-world-java/
├── src/
│   ├── main/java/com/example/
│   │   └── App.java               ← HTTP server (/, /health, /greet)
│   └── test/java/com/example/
│       └── AppTest.java           ← JUnit 5 unit tests (8 tests)
├── pom.xml                        ← Maven, JUnit 5 only
├── perf-config.yaml               ← Performance test configuration
├── pipeline_runner.py             ← Calls perf agent service
├── requirements-perf.txt          ← Python deps for pipeline runner
└── .github/workflows/
    └── ci.yml                     ← Full CI/CD pipeline
```

---

## API Endpoints

| Method | Endpoint        | Response                              |
|--------|-----------------|---------------------------------------|
| GET    | /               | `{"message":"Hello World!","version":"1.0.0"}` |
| GET    | /health         | `{"status":"UP","service":"hello-world-api"}` |
| GET    | /greet?name=X   | `{"message":"Hello, X!"}`             |

---

## Local Setup

### Run the Java app
```cmd
mvn clean package
java -jar target/hello-world-1.0.0.jar
```

### Run unit tests only
```cmd
mvn clean test
```

### Run performance test locally (requires perf agent service running)
```cmd
set PERF_AGENT_URL=http://localhost:8000
set API_AUTH_PASSWORD=password123
python pipeline_runner.py --config perf-config.yaml --dry-run
```

---

## CI/CD Pipeline

The pipeline has two jobs:

```
Push code
    ↓
Job 1: Unit Tests (Maven)
    ├── mvn clean test
    └── Upload surefire reports
         ↓ (only if unit tests pass)
Job 2: Performance Tests
    ├── Detect spec change
    ├── python pipeline_runner.py
    ├── Upload HTML report artifact
    └── Comment on PR with results
```

---

## GitHub Secrets Required

Set these in your repo:
**Settings → Secrets and Variables → Actions → New repository secret**

| Secret Name        | Value                              |
|--------------------|------------------------------------|
| `PERF_AGENT_URL`   | Your ngrok URL or server URL       |
| `API_AUTH_PASSWORD`| `password123` (restful-booker)     |

---

## How to trigger performance tests manually

Go to **Actions → CI Pipeline → Run workflow**
- Select profile: `baseline` / `load` / `stress` / `spike` / `soak`
- Optionally skip performance tests

---

## Changing the load profile

Edit `perf-config.yaml`:
```yaml
profile: "load"      # change from baseline to load

load:

## Updated by Raghav
  threads     : 20   # increase users
  ramp_up_sec : 40
  duration_sec: 300
```

Commit and push — pipeline picks up new config automatically.
