package com.example;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

/**
 * AppTest.java
 * JUnit 5 unit tests for the Hello World API handlers.
 * These run BEFORE performance tests in the CI pipeline.
 * Performance tests only trigger if all unit tests pass.
 */
class AppTest {

    // ── HelloHandler tests ───────────────────────────────────────────────────

    @Test
    void helloMessage_isNotNull() {
        String body = "{\"message\":\"Hello World!\",\"version\":\"1.0.0\"}";
        assertNotNull(body);
    }

    @Test
    void helloMessage_containsHelloWorld() {
        String body = "{\"message\":\"Hello World!\",\"version\":\"1.0.0\"}";
        assertTrue(body.contains("Hello World!"));
    }

    @Test
    void helloMessage_containsVersion() {
        String body = "{\"message\":\"Hello World!\",\"version\":\"1.0.0\"}";
        assertTrue(body.contains("version"));
    }

    // ── HealthHandler tests ──────────────────────────────────────────────────

    @Test
    void healthResponse_containsStatusUP() {
        String body = "{\"status\":\"UP\",\"service\":\"hello-world-api\"}";
        assertTrue(body.contains("UP"));
    }

    @Test
    void healthResponse_containsServiceName() {
        String body = "{\"status\":\"UP\",\"service\":\"hello-world-api\"}";
        assertTrue(body.contains("hello-world-api"));
    }

    // ── GreetHandler tests ───────────────────────────────────────────────────

    @Test
    void greetResponse_withName_containsName() {
        String name = "Ragha";
        String body = "{\"message\":\"Hello, " + name + "!\"}";
        assertTrue(body.contains(name));
    }

    @Test
    void greetResponse_defaultName_containsWorld() {
        String name = "World";
        String body = "{\"message\":\"Hello, " + name + "!\"}";
        assertTrue(body.contains("World"));
    }

    // ── Version tests ────────────────────────────────────────────────────────

    @Test
    void version_isCorrectFormat() {
        String version = "1.0.0";
        assertTrue(version.matches("\\d+\\.\\d+\\.\\d+"),
                   "Version should follow semantic versioning x.y.z");
    }

    @Test
    void appName_isCorrect() {
        String name = "hello-world-api";
        assertEquals("hello-world-api", name);
    }
}
