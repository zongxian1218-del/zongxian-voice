package com.zongxian.transfer;

import android.content.res.AssetManager;
import android.util.Log;

import java.io.BufferedInputStream;
import java.io.BufferedOutputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;
import java.util.Map;
import java.util.Random;
import java.util.concurrent.Semaphore;

/**
 * Tiny loopback-only HTTP/1.1 server.
 *
 * <p>Serves {@code assets/web/**} over {@code http://127.0.0.1:<port>/} so the WebView gets a
 * <em>secure context</em> (required for {@code crypto.subtle}) without touching the filesystem.
 * Also accepts {@code POST /__save?name=<urlencoded>} and streams the raw request body into the
 * public Downloads collection via {@link SaveSink}.
 *
 * <p>No third-party libraries, no {@code shouldInterceptRequest} override.
 */
public final class LocalHttpServer {

  private static final String TAG = "ZongxianHttp";
  private static final String ASSET_ROOT = "web/";
  private static final int PORT_MIN = 20000;
  private static final int PORT_MAX = 40000; // exclusive
  private static final int SOCKET_TIMEOUT_MS = 60000;
  private static final int MAX_CONCURRENT = 24;
  private static final int COPY_BUFFER = 1 << 16;
  private static final long MAX_CACHED_ASSET = 16L * 1024 * 1024;

  /** Where a received file body should land. */
  public interface SaveSink {
    /**
     * Streams {@code body} into the public Downloads location.
     *
     * @param fileName      already sanitised leaf name
     * @param body          body stream (bounded by {@code contentLength} when >= 0)
     * @param contentLength declared length, or {@code -1} for chunked bodies
     * @return the absolute on-device path of the written file
     */
    String save(String fileName, InputStream body, long contentLength) throws IOException;
  }

  private final AssetManager assets;
  private final SaveSink saver;
  private final Semaphore slots = new Semaphore(MAX_CONCURRENT);
  private final Map<String, byte[]> cache = new HashMap<String, byte[]>();
  private final Object cacheLock = new Object();

  private volatile ServerSocket serverSocket;
  private volatile boolean running;
  private volatile int port = -1;
  private Thread acceptThread;

  public LocalHttpServer(AssetManager assets, SaveSink saver) {
    this.assets = assets;
    this.saver = saver;
  }

  /** URL the WebView should load. */
  public String pageUrl() {
    return "http://127.0.0.1:" + port + "/index.html";
  }

  /** URL the page posts received files to. Empty when the server is not running. */
  public String saveUrl() {
    return port > 0 ? "http://127.0.0.1:" + port + "/__save" : "";
  }

  public int getPort() {
    return port;
  }

  /**
   * Binds a free loopback port in {@code [20000, 40000)} and starts the accept loop.
   *
   * @return the bound port
   * @throws IOException when no port in the range could be bound
   */
  public int start() throws IOException {
    IOException last = null;
    Random rnd = new Random();
    int span = PORT_MAX - PORT_MIN;
    for (int i = 0; i < 400; i++) {
      int candidate = PORT_MIN + rnd.nextInt(span);
      ServerSocket ss = new ServerSocket();
      try {
        ss.setReuseAddress(false);
        // 必须显式绑定 IPv4 的 127.0.0.1。
        // Android 上 InetAddress.getLoopbackAddress() 返回的是 IPv6 的 ::1，
        // 只绑 ::1 时 WebView 去连 http://127.0.0.1:port/ 会直接 ERR_CONNECTION_REFUSED。
        ss.bind(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), candidate), 64);
      } catch (IOException e) {
        last = e;
        try {
          ss.close();
        } catch (IOException ignored) {
          // nothing to do
        }
        continue;
      }
      // 自检：真的连一次，确认 127.0.0.1 上确实连得通（只绑上但连不进的情况要换端口）
      if (!selfCheck(candidate)) {
        last = new IOException("bound 127.0.0.1:" + candidate + " but self-connect failed");
        try {
          ss.close();
        } catch (IOException ignored) {
          // nothing to do
        }
        continue;
      }
      serverSocket = ss;
      port = candidate;
      running = true;
      acceptThread = new Thread(new Runnable() {
        @Override
        public void run() {
          acceptLoop();
        }
      }, "zongxian-http-accept");
      acceptThread.setDaemon(true);
      acceptThread.start();
      Log.i(TAG, "listening on 127.0.0.1:" + candidate + " (self-check ok)");
      return candidate;
    }
    throw new IOException("no free loopback port in [" + PORT_MIN + "," + PORT_MAX + ")", last);
  }

  /** 连一次自己，确认 WebView 用的那个地址真的可达。 */
  private static boolean selfCheck(int port) {
    Socket s = new Socket();
    try {
      s.connect(new InetSocketAddress(InetAddress.getByName("127.0.0.1"), port), 1500);
      return true;
    } catch (IOException e) {
      return false;
    } finally {
      try {
        s.close();
      } catch (IOException ignored) {
        // nothing to do
      }
    }
  }

  public void stop() {
    running = false;
    ServerSocket ss = serverSocket;
    serverSocket = null;
    if (ss != null) {
      try {
        ss.close();
      } catch (IOException ignored) {
        // nothing to do
      }
    }
  }

  private void acceptLoop() {
    while (running) {
      final Socket socket;
      try {
        socket = serverSocket.accept();
      } catch (IOException e) {
        if (running) {
          Log.w(TAG, "accept failed: " + e);
        }
        break;
      }
      // Reject anything that is somehow not from this device.
      InetAddress remote = socket.getInetAddress();
      if (remote != null && !remote.isLoopbackAddress()) {
        closeQuietly(socket);
        continue;
      }
      if (!slots.tryAcquire()) {
        closeQuietly(socket);
        continue;
      }
      Thread worker = new Thread(new Runnable() {
        @Override
        public void run() {
          try {
            handle(socket);
          } catch (Throwable t) {
            Log.w(TAG, "connection error: " + t, t);
          } finally {
            closeQuietly(socket);
            slots.release();
          }
        }
      }, "zongxian-http-conn");
      worker.setDaemon(true);
      worker.start();
    }
  }

  // ------------------------------------------------------------------ request

  private void handle(Socket socket) throws IOException {
    socket.setSoTimeout(SOCKET_TIMEOUT_MS);
    socket.setTcpNoDelay(true);
    BufferedInputStream in = new BufferedInputStream(socket.getInputStream(), COPY_BUFFER);
    BufferedOutputStream out = new BufferedOutputStream(socket.getOutputStream(), COPY_BUFFER);

    String requestLineRaw = readLine(in);
    if (requestLineRaw == null) {
      return; // client hung up / closed a preconnect
    }
    HttpCore.RequestLine req = HttpCore.parseRequestLine(requestLineRaw);
    if (req == null) {
      sendError(out, 400, "Bad Request", "malformed request line");
      return;
    }

    List<String> headerLines = new ArrayList<String>();
    String line;
    int guard = 0;
    while ((line = readLine(in)) != null) {
      if (line.isEmpty()) {
        break;
      }
      if (++guard > HttpCore.MAX_HEADERS) {
        sendError(out, 431, "Request Header Fields Too Large", "too many headers");
        return;
      }
      headerLines.add(line);
    }
    LinkedHashMap<String, List<String>> headers = HttpCore.parseHeaders(headerLines);

    if ("/__save".equals(stripQuery(req.rawPath)) || "/__save/".equals(stripQuery(req.rawPath))) {
      if ("POST".equals(req.method)) {
        handleSave(req, headers, in, out);
      } else if ("OPTIONS".equals(req.method)) {
        sendStatus(out, 204, "No Content", null, null);
      } else {
        sendError(out, 405, "Method Not Allowed", "POST only");
      }
      return;
    }

    if ("GET".equals(req.method) || "HEAD".equals(req.method)) {
      handleStatic(req, headers, out, "HEAD".equals(req.method));
      return;
    }
    if ("OPTIONS".equals(req.method)) {
      sendStatus(out, 204, "No Content", null, null);
      return;
    }
    sendError(out, 405, "Method Not Allowed", "only GET, HEAD and POST /__save");
  }

  private static String stripQuery(String rawPath) {
    int q = rawPath == null ? -1 : rawPath.indexOf('?');
    return q < 0 ? rawPath : rawPath.substring(0, q);
  }

  // ------------------------------------------------------------------- static

  private void handleStatic(HttpCore.RequestLine req, LinkedHashMap<String, List<String>> headers,
                            OutputStream out, boolean headOnly) throws IOException {
    String rel = HttpCore.assetPath(req.rawPath);
    if (rel == null) {
      sendError(out, 400, "Bad Request", "path rejected");
      return;
    }
    byte[] body = loadAsset(rel);
    if (body == null) {
      sendError(out, 404, "Not Found", rel);
      return;
    }
    String mime = HttpCore.mimeType(rel);
    long size = body.length;
    long[] range = HttpCore.parseRange(HttpCore.header(headers, "range"), size);

    if (range != null) {
      long start = range[0];
      long end = range[1];
      long len = end - start + 1;
      StringBuilder sb = new StringBuilder();
      sb.append("HTTP/1.1 206 Partial Content\r\n");
      sb.append("Content-Type: ").append(mime).append("\r\n");
      sb.append("Content-Length: ").append(len).append("\r\n");
      sb.append("Content-Range: bytes ").append(start).append('-').append(end).append('/')
          .append(size).append("\r\n");
      sb.append("Accept-Ranges: bytes\r\n");
      sb.append("Cache-Control: no-store\r\n");
      sb.append("Connection: close\r\n\r\n");
      out.write(sb.toString().getBytes("ISO-8859-1"));
      if (!headOnly) {
        out.write(body, (int) start, (int) len);
      }
      out.flush();
      return;
    }

    StringBuilder sb = new StringBuilder();
    sb.append("HTTP/1.1 200 OK\r\n");
    sb.append("Content-Type: ").append(mime).append("\r\n");
    sb.append("Content-Length: ").append(size).append("\r\n");
    sb.append("Accept-Ranges: bytes\r\n");
    sb.append("Cache-Control: no-store\r\n");
    sb.append("Connection: close\r\n\r\n");
    out.write(sb.toString().getBytes("ISO-8859-1"));
    if (!headOnly) {
      out.write(body);
    }
    out.flush();
  }

  /** Reads an asset fully (single-file app: a few hundred KB at most). */
  private byte[] loadAsset(String rel) {
    synchronized (cacheLock) {
      byte[] hit = cache.get(rel);
      if (hit != null) {
        return hit;
      }
    }
    InputStream is = null;
    try {
      is = assets.open(ASSET_ROOT + rel, AssetManager.ACCESS_STREAMING);
      ByteArrayOutputStream bos = new ByteArrayOutputStream(1 << 16);
      byte[] buf = new byte[COPY_BUFFER];
      int n;
      while ((n = is.read(buf)) > 0) {
        bos.write(buf, 0, n);
      }
      byte[] data = bos.toByteArray();
      if (data.length <= MAX_CACHED_ASSET) {
        synchronized (cacheLock) {
          cache.put(rel, data);
        }
      }
      return data;
    } catch (IOException e) {
      return null;
    } finally {
      if (is != null) {
        try {
          is.close();
        } catch (IOException ignored) {
          // nothing to do
        }
      }
    }
  }

  // ------------------------------------------------------------------- __save

  private void handleSave(HttpCore.RequestLine req, LinkedHashMap<String, List<String>> headers,
                          InputStream in, OutputStream out) throws IOException {
    String name = HttpCore.queryParam(req.rawQuery, "name");
    String fileName = HttpCore.safeFileName(name);

    if (HttpCore.headerHasToken(headers, "expect", "100-continue")) {
      out.write("HTTP/1.1 100 Continue\r\n\r\n".getBytes("ISO-8859-1"));
      out.flush();
    }

    InputStream body;
    long declared;
    if (HttpCore.headerHasToken(headers, "transfer-encoding", "chunked")) {
      body = new HttpCore.ChunkedInputStream(in);
      declared = -1L;
    } else {
      long cl = HttpCore.parseDecimal(HttpCore.header(headers, "content-length"));
      if (cl < 0) {
        sendError(out, 411, "Length Required", "missing Content-Length or chunked encoding");
        return;
      }
      declared = cl;
      body = new BoundedInputStream(in, cl);
    }

    try {
      String path = saver.save(fileName, body, declared);
      String json = "{\"ok\":true,\"path\":\"" + HttpCore.jsonEscape(path) + "\"}";
      sendJson(out, 200, "OK", json);
      Log.i(TAG, "saved " + declared + " bytes -> " + path);
    } catch (IOException e) {
      Log.w(TAG, "save failed: " + e, e);
      String json = "{\"ok\":false,\"error\":\"" + HttpCore.jsonEscape(String.valueOf(e.getMessage()))
          + "\"}";
      sendJson(out, 500, "Internal Server Error", json);
    } catch (RuntimeException e) {
      Log.w(TAG, "save failed: " + e, e);
      String json = "{\"ok\":false,\"error\":\"" + HttpCore.jsonEscape(String.valueOf(e))
          + "\"}";
      sendJson(out, 500, "Internal Server Error", json);
    }
  }

  /** Hard-bounded view of the socket stream (used for the Content-Length body). */
  private static final class BoundedInputStream extends InputStream {
    private final InputStream in;
    private long left;

    BoundedInputStream(InputStream in, long limit) {
      this.in = in;
      this.left = limit;
    }

    @Override
    public int read() throws IOException {
      if (left <= 0) {
        return -1;
      }
      int c = in.read();
      if (c >= 0) {
        left--;
      }
      return c;
    }

    @Override
    public int read(byte[] b, int off, int len) throws IOException {
      if (left <= 0) {
        return -1;
      }
      int want = (int) Math.min((long) len, left);
      int n = in.read(b, off, want);
      if (n > 0) {
        left -= n;
      }
      return n;
    }

    @Override
    public void close() {
      // the socket is closed by the caller
    }
  }

  // ------------------------------------------------------------------ responses

  private static void sendJson(OutputStream out, int code, String reason, String json)
      throws IOException {
    byte[] body = json.getBytes("UTF-8");
    StringBuilder sb = new StringBuilder();
    sb.append("HTTP/1.1 ").append(code).append(' ').append(reason).append("\r\n");
    sb.append("Content-Type: application/json; charset=utf-8\r\n");
    sb.append("Content-Length: ").append(body.length).append("\r\n");
    sb.append("Cache-Control: no-store\r\n");
    sb.append("Connection: close\r\n\r\n");
    out.write(sb.toString().getBytes("ISO-8859-1"));
    out.write(body);
    out.flush();
  }

  private static void sendError(OutputStream out, int code, String reason, String detail)
      throws IOException {
    String json = "{\"ok\":false,\"error\":\"" + HttpCore.jsonEscape(detail) + "\"}";
    sendJson(out, code, reason, json);
  }

  private static void sendStatus(OutputStream out, int code, String reason, String extraHeaders,
                                 String body) throws IOException {
    StringBuilder sb = new StringBuilder();
    sb.append("HTTP/1.1 ").append(code).append(' ').append(reason).append("\r\n");
    if (extraHeaders != null) {
      sb.append(extraHeaders);
    }
    sb.append("Content-Length: 0\r\n");
    sb.append("Access-Control-Allow-Methods: POST, GET, HEAD, OPTIONS\r\n");
    sb.append("Access-Control-Allow-Headers: Content-Type\r\n");
    sb.append("Cache-Control: no-store\r\n");
    sb.append("Connection: close\r\n\r\n");
    out.write(sb.toString().getBytes("ISO-8859-1"));
    out.flush();
  }

  // -------------------------------------------------------------------- plumbing

  /** Reads one CRLF/LF-terminated line as US-ASCII (headers are latin-1-ish). */
  private static String readLine(InputStream in) throws IOException {
    ByteArrayOutputStream buf = new ByteArrayOutputStream(128);
    int c;
    boolean any = false;
    while ((c = in.read()) >= 0) {
      any = true;
      if (c == '\n') {
        break;
      }
      if (c == '\r') {
        continue;
      }
      if (buf.size() >= HttpCore.MAX_LINE) {
        throw new IOException("header line too long");
      }
      buf.write(c);
    }
    if (!any && buf.size() == 0) {
      return null;
    }
    return new String(buf.toByteArray(), "ISO-8859-1");
  }

  private static void closeQuietly(Socket s) {
    try {
      s.close();
    } catch (IOException ignored) {
      // nothing to do
    }
  }

  /** Locale-independent percentage used only for logging elsewhere. */
  static String fmt(long bytes) {
    return String.format(Locale.US, "%.2f MiB", bytes / 1048576.0);
  }
}
