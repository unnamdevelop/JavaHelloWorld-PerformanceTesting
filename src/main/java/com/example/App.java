package com.example;

import com.sun.net.httpserver.HttpServer;
import com.sun.net.httpserver.HttpHandler;
import com.sun.net.httpserver.HttpExchange;

import java.io.IOException;
import java.io.OutputStream;
import java.net.InetSocketAddress;

/**
 * App.java
 * Simple Java HTTP server — no frameworks, no dependencies.
 * Uses built-in com.sun.net.httpserver (available in all JDKs).
 *
 * Endpoints:
 *   GET /         → Hello World message
 *   GET /health   → Service health status
 *   GET /greet    → Personalised greeting
 */
public class App {

    public static void main(String[] args) throws IOException {
        int port = 8080;
        HttpServer server = HttpServer.create(new InetSocketAddress(port), 0);

        // Register endpoints
        server.createContext("/",       new HelloHandler());
        server.createContext("/health", new HealthHandler());
        server.createContext("/greet",  new GreetHandler());

        server.setExecutor(null);   // default executor
        server.start();

        System.out.println("Hello World API running on port " + port);
        System.out.println("  GET http://localhost:" + port + "/");
        System.out.println("  GET http://localhost:" + port + "/health");
        System.out.println("  GET http://localhost:" + port + "/greet?name=Ragha");
    }

    // ── Handlers ─────────────────────────────────────────────────────────────

    static class HelloHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (!"GET".equals(exchange.getRequestMethod())) {
                sendResponse(exchange, 405, "{\"error\":\"Method Not Allowed\"}");
                return;
            }
            String body = "{\"message\":\"Hello World!\",\"version\":\"1.0.0\"}";
            sendResponse(exchange, 200, body);
        }
    }

    static class HealthHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (!"GET".equals(exchange.getRequestMethod())) {
                sendResponse(exchange, 405, "{\"error\":\"Method Not Allowed\"}");
                return;
            }
            String body = "{\"status\":\"UP\",\"service\":\"hello-world-api\"}";
            sendResponse(exchange, 200, body);
        }
    }

    static class GreetHandler implements HttpHandler {
        @Override
        public void handle(HttpExchange exchange) throws IOException {
            if (!"GET".equals(exchange.getRequestMethod())) {
                sendResponse(exchange, 405, "{\"error\":\"Method Not Allowed\"}");
                return;
            }
            // Parse ?name=xxx query param
            String query = exchange.getRequestURI().getQuery();
            String name  = "World";
            if (query != null && query.startsWith("name=")) {
                name = query.substring(5);
            }
            String body = "{\"message\":\"Hello, " + name + "!\"}";
            sendResponse(exchange, 200, body);
        }
    }

    // ── Shared helper ─────────────────────────────────────────────────────────

    static void sendResponse(HttpExchange exchange,
                              int statusCode,
                              String body) throws IOException {
        exchange.getResponseHeaders().set("Content-Type", "application/json");
        byte[] bytes = body.getBytes();
        exchange.sendResponseHeaders(statusCode, bytes.length);
        try (OutputStream os = exchange.getResponseBody()) {
            os.write(bytes);
        }
    }
}
