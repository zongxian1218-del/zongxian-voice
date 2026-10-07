package com.zongxian.transfer;

import java.io.IOException;
import java.io.InputStream;
import java.io.UnsupportedEncodingException;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Locale;

/**
 * Pure-Java HTTP/1.1 helpers used by {@link LocalHttpServer}.
 *
 * <p>Deliberately free of any {@code android.*} import so that the unit test
 * ({@code HttpCoreTest}) can compile and run this class on a plain desktop JVM.
 * Everything here is deterministic and side-effect free.
 */
public final class HttpCore {

  public static final int MAX_LINE = 8192;
  public static final int MAX_HEADERS = 128;
  public static final int MAX_FILE_NAME = 180;

  private HttpCore() {
  }

  // ---------------------------------------------------------------- request line

  /** Parsed HTTP request line. */
  public static final class RequestLine {
    public final String method;
    public final String target;
    /** Percent-encoded path without the query string. */
    public final String rawPath;
    /** Raw query string, or {@code null} when the target had no '?'. */
    public final String rawQuery;
    public final String version;

    RequestLine(String method, String target, String rawPath, String rawQuery, String version) {
      this.method = method;
      this.target = target;
      this.rawPath = rawPath;
      this.rawQuery = rawQuery;
      this.version = version;
    }

    @Override
    public String toString() {
      return method + " " + target + " " + version;
    }
  }

  /**
   * Parses {@code METHOD SP request-target SP HTTP-version}.
   *
   * @return the parsed line, or {@code null} when the line is not a valid request line.
   */
  public static RequestLine parseRequestLine(String line) {
    if (line == null) {
      return null;
    }
    String s = line.trim();
    if (s.isEmpty()) {
      return null;
    }
    int first = s.indexOf(' ');
    if (first <= 0) {
      return null;
    }
    int last = s.lastIndexOf(' ');
    if (last <= first) {
      return null;
    }
    String method = s.substring(0, first).toUpperCase(Locale.US);
    String target = s.substring(first + 1, last).trim();
    String version = s.substring(last + 1).trim();
    if (target.isEmpty() || !version.toUpperCase(Locale.US).startsWith("HTTP/")) {
      return null;
    }
    if (!isToken(method)) {
      return null;
    }
    // absolute-form: GET http://127.0.0.1:8080/index.html HTTP/1.1
    String pathAndQuery = target;
    int scheme = pathAndQuery.indexOf("://");
    if (scheme > 0) {
      int slash = pathAndQuery.indexOf('/', scheme + 3);
      pathAndQuery = slash < 0 ? "/" : pathAndQuery.substring(slash);
    }
    String rawPath;
    String rawQuery;
    int q = pathAndQuery.indexOf('?');
    if (q < 0) {
      rawPath = pathAndQuery;
      rawQuery = null;
    } else {
      rawPath = pathAndQuery.substring(0, q);
      rawQuery = pathAndQuery.substring(q + 1);
    }
    if (rawPath.isEmpty()) {
      rawPath = "/";
    }
    return new RequestLine(method, target, rawPath, rawQuery, version);
  }

  private static boolean isToken(String s) {
    if (s.isEmpty()) {
      return false;
    }
    for (int i = 0; i < s.length(); i++) {
      char c = s.charAt(i);
      boolean ok = (c >= 'A' && c <= 'Z') || (c >= '0' && c <= '9') || "-_.!*'%".indexOf(c) >= 0;
      if (!ok) {
        return false;
      }
    }
    return true;
  }

  // -------------------------------------------------------------------- headers

  /** Parses header lines into a lower-cased multimap, folding obs-fold continuations. */
  public static LinkedHashMap<String, List<String>> parseHeaders(List<String> lines) {
    LinkedHashMap<String, List<String>> out = new LinkedHashMap<String, List<String>>();
    String currentName = null;
    for (String raw : lines) {
      if (raw == null) {
        continue;
      }
      String line = raw;
      if ((line.startsWith(" ") || line.startsWith("\t")) && currentName != null) {
        List<String> vals = out.get(currentName);
        if (vals != null && !vals.isEmpty()) {
          vals.set(vals.size() - 1, vals.get(vals.size() - 1) + " " + line.trim());
        }
        continue;
      }
      int colon = line.indexOf(':');
      if (colon <= 0) {
        continue;
      }
      String name = line.substring(0, colon).trim().toLowerCase(Locale.US);
      String value = line.substring(colon + 1).trim();
      if (name.isEmpty()) {
        continue;
      }
      List<String> vals = out.get(name);
      if (vals == null) {
        vals = new ArrayList<String>(1);
        out.put(name, vals);
      }
      vals.add(value);
      currentName = name;
    }
    return out;
  }

  /** First value for {@code name}, or {@code null}. */
  public static String header(LinkedHashMap<String, List<String>> headers, String name) {
    List<String> vals = headers.get(name.toLowerCase(Locale.US));
    return (vals == null || vals.isEmpty()) ? null : vals.get(0);
  }

  /** True when any comma-separated token of the header equals {@code token}. */
  public static boolean headerHasToken(LinkedHashMap<String, List<String>> headers,
                                      String name, String token) {
    List<String> vals = headers.get(name.toLowerCase(Locale.US));
    if (vals == null) {
      return false;
    }
    for (String v : vals) {
      for (String part : v.split(",")) {
        if (part.trim().equalsIgnoreCase(token)) {
          return true;
        }
      }
    }
    return false;
  }

  /** Parses a non-negative decimal, returning {@code -1} on garbage. */
  public static long parseDecimal(String s) {
    if (s == null) {
      return -1L;
    }
    String t = s.trim();
    if (t.isEmpty() || t.length() > 19) {
      return -1L;
    }
    long v = 0L;
    for (int i = 0; i < t.length(); i++) {
      char c = t.charAt(i);
      if (c < '0' || c > '9') {
        return -1L;
      }
      v = v * 10L + (c - '0');
    }
    return v;
  }

  // -------------------------------------------------------------- url decoding

  /**
   * Percent-decodes {@code s} as UTF-8.
   *
   * @param plusAsSpace whether '+' means a space (query-string rules) or a literal '+'
   *                    (path rules).
   * @return decoded string, or {@code null} when an escape sequence is invalid.
   */
  public static String urlDecode(String s, boolean plusAsSpace) {
    if (s == null) {
      return null;
    }
    int n = s.length();
    // Worst case is one multi-byte UTF-8 sequence per input char (3 bytes for a BMP
    // char); surrogate pairs produce 4 bytes for 2 chars, so 3*n+8 always suffices.
    byte[] buf = new byte[n * 3 + 8];
    int len = 0;
    for (int i = 0; i < n; i++) {
      char c = s.charAt(i);
      if (c == '%') {
        if (i + 2 >= n) {
          return null;
        }
        int hi = hex(s.charAt(i + 1));
        int lo = hex(s.charAt(i + 2));
        if (hi < 0 || lo < 0) {
          return null;
        }
        buf[len++] = (byte) ((hi << 4) | lo);
        i += 2;
      } else if (c == '+' && plusAsSpace) {
        buf[len++] = (byte) ' ';
      } else if (c < 0x80) {
        buf[len++] = (byte) c;
      } else {
        // Re-encode the UTF-16 char as UTF-8 without pulling in android classes.
        byte[] enc;
        if (Character.isHighSurrogate(c)) {
          if (i + 1 >= n || !Character.isLowSurrogate(s.charAt(i + 1))) {
            return null;
          }
          int cp = Character.toCodePoint(c, s.charAt(i + 1));
          enc = new byte[]{
              (byte) (0xF0 | (cp >> 18)),
              (byte) (0x80 | ((cp >> 12) & 0x3F)),
              (byte) (0x80 | ((cp >> 6) & 0x3F)),
              (byte) (0x80 | (cp & 0x3F))};
          i++;
        } else if (Character.isLowSurrogate(c)) {
          return null;
        } else if (c < 0x800) {
          enc = new byte[]{(byte) (0xC0 | (c >> 6)), (byte) (0x80 | (c & 0x3F))};
        } else {
          enc = new byte[]{(byte) (0xE0 | (c >> 12)), (byte) (0x80 | ((c >> 6) & 0x3F)),
              (byte) (0x80 | (c & 0x3F))};
        }
        if (len + enc.length > buf.length) {
          byte[] bigger = new byte[len + enc.length + 8];
          System.arraycopy(buf, 0, bigger, 0, len);
          buf = bigger;
        }
        System.arraycopy(enc, 0, buf, len, enc.length);
        len += enc.length;
      }
    }
    try {
      return new String(buf, 0, len, "UTF-8");
    } catch (UnsupportedEncodingException e) {
      return null;
    }
  }

  private static int hex(char c) {
    if (c >= '0' && c <= '9') {
      return c - '0';
    }
    if (c >= 'a' && c <= 'f') {
      return c - 'a' + 10;
    }
    if (c >= 'A' && c <= 'F') {
      return c - 'A' + 10;
    }
    return -1;
  }

  /** Extracts a form-encoded query parameter; returns {@code null} when absent. */
  public static String queryParam(String rawQuery, String key) {
    if (rawQuery == null || key == null) {
      return null;
    }
    for (String pair : rawQuery.split("&")) {
      if (pair.isEmpty()) {
        continue;
      }
      int eq = pair.indexOf('=');
      String k = eq < 0 ? pair : pair.substring(0, eq);
      String v = eq < 0 ? "" : pair.substring(eq + 1);
      String dk = urlDecode(k, true);
      if (dk != null && dk.equals(key)) {
        String dv = urlDecode(v, true);
        return dv == null ? null : dv;
      }
    }
    return null;
  }

  // ------------------------------------------------------------- path -> asset

  /**
   * Maps a request path to a path relative to the APK's {@code assets/web/} root.
   *
   * <p>Rejects traversal ({@code ..}), NUL bytes and Windows separators; maps
   * {@code /} and directory requests to {@code index.html}.
   *
   * @return the relative asset path, or {@code null} when the request must be refused.
   */
  public static String assetPath(String rawPath) {
    if (rawPath == null) {
      return null;
    }
    String decoded = urlDecode(rawPath, false);
    if (decoded == null) {
      return null;
    }
    if (decoded.indexOf('\0') >= 0) {
      return null;
    }
    decoded = decoded.replace('\\', '/');
    if (!decoded.startsWith("/")) {
      decoded = "/" + decoded;
    }
    boolean dir = decoded.endsWith("/");
    String[] parts = decoded.split("/", -1);
    StringBuilder sb = new StringBuilder();
    for (String part : parts) {
      if (part.isEmpty() || ".".equals(part)) {
        continue;
      }
      if ("..".equals(part)) {
        return null;
      }
      // Refuse a leading '../' that survived the split and any drive-ish prefix.
      if (part.indexOf(':') >= 0) {
        return null;
      }
      if (sb.length() > 0) {
        sb.append('/');
      }
      sb.append(part);
    }
    String rel = sb.toString();
    if (rel.isEmpty() || dir) {
      rel = rel.isEmpty() ? "index.html" : rel + "/index.html";
    }
    if (rel.startsWith("/")) {
      return null;
    }
    return rel;
  }

  /** MIME type guess from the file extension, never {@code null}. */
  public static String mimeType(String path) {
    String p = path == null ? "" : path.toLowerCase(Locale.US);
    int dot = p.lastIndexOf('.');
    String ext = dot < 0 ? "" : p.substring(dot + 1);
    if ("html".equals(ext) || "htm".equals(ext)) {
      return "text/html; charset=utf-8";
    }
    if ("js".equals(ext) || "mjs".equals(ext)) {
      return "text/javascript; charset=utf-8";
    }
    if ("css".equals(ext)) {
      return "text/css; charset=utf-8";
    }
    if ("json".equals(ext) || "map".equals(ext)) {
      return "application/json; charset=utf-8";
    }
    if ("txt".equals(ext) || "md".equals(ext) || "log".equals(ext)) {
      return "text/plain; charset=utf-8";
    }
    if ("svg".equals(ext)) {
      return "image/svg+xml";
    }
    if ("png".equals(ext)) {
      return "image/png";
    }
    if ("jpg".equals(ext) || "jpeg".equals(ext)) {
      return "image/jpeg";
    }
    if ("gif".equals(ext)) {
      return "image/gif";
    }
    if ("webp".equals(ext)) {
      return "image/webp";
    }
    if ("ico".equals(ext)) {
      return "image/x-icon";
    }
    if ("woff2".equals(ext)) {
      return "font/woff2";
    }
    if ("woff".equals(ext)) {
      return "font/woff";
    }
    if ("ttf".equals(ext)) {
      return "font/ttf";
    }
    if ("wasm".equals(ext)) {
      return "application/wasm";
    }
    if ("mp4".equals(ext)) {
      return "video/mp4";
    }
    if ("mp3".equals(ext)) {
      return "audio/mpeg";
    }
    if ("wav".equals(ext)) {
      return "audio/wav";
    }
    if ("zip".equals(ext)) {
      return "application/zip";
    }
    return "application/octet-stream";
  }

  // -------------------------------------------------------------------- ranges

  /**
   * Parses a single-range {@code Range} header against a known resource size.
   *
   * @return {@code {start, endInclusive}}, or {@code null} when the header must be
   *         ignored (unsupported unit, malformed, multi-range) or is unsatisfiable.
   */
  public static long[] parseRange(String headerValue, long size) {
    if (headerValue == null || size < 0) {
      return null;
    }
    String v = headerValue.trim();
    if (!v.toLowerCase(Locale.US).startsWith("bytes=")) {
      return null;
    }
    String spec = v.substring(6).trim();
    if (spec.indexOf(',') >= 0) {
      return null; // multi-range: answer with the full body instead
    }
    int dash = spec.indexOf('-');
    if (dash < 0) {
      return null;
    }
    String a = spec.substring(0, dash).trim();
    String b = spec.substring(dash + 1).trim();
    if (a.isEmpty()) {
      long suffix = parseDecimal(b);
      if (suffix <= 0) {
        return null;
      }
      if (size == 0) {
        return null;
      }
      long start = Math.max(0L, size - suffix);
      return new long[]{start, size - 1};
    }
    long start = parseDecimal(a);
    if (start < 0) {
      return null;
    }
    long end;
    if (b.isEmpty()) {
      end = size - 1;
    } else {
      end = parseDecimal(b);
      if (end < 0) {
        return null;
      }
    }
    if (size == 0) {
      return null;
    }
    if (start >= size) {
      return null; // 416 territory; caller falls back to 200
    }
    if (end >= size) {
      end = size - 1;
    }
    if (end < start) {
      return null;
    }
    return new long[]{start, end};
  }

  // ------------------------------------------------------------- chunked body

  /** Decoding stream for {@code Transfer-Encoding: chunked} request bodies. */
  public static final class ChunkedInputStream extends InputStream {
    private final InputStream in;
    private long remaining;
    private boolean done;
    private boolean first = true;
    private byte[] lineBuf = new byte[64];
    private int lineLen;

    public ChunkedInputStream(InputStream in) {
      this.in = in;
    }

    /** Reads one CRLF-terminated line; empty string means "no size line yet". */
    private String readLine() throws IOException {
      lineLen = 0;
      while (true) {
        int c = in.read();
        if (c < 0) {
          if (lineLen == 0) {
            return null;
          }
          break;
        }
        if (c == '\n') {
          break;
        }
        if (c == '\r') {
          continue;
        }
        if (lineLen >= lineBuf.length) {
          byte[] bigger = new byte[lineBuf.length * 2];
          System.arraycopy(lineBuf, 0, bigger, 0, lineLen);
          lineBuf = bigger;
        }
        lineBuf[lineLen++] = (byte) c;
      }
      return new String(lineBuf, 0, lineLen, "US-ASCII");
    }

    private void nextChunk() throws IOException {
      if (!first) {
        String trailer = readLine();
        while (trailer != null && !trailer.isEmpty()) {
          trailer = readLine();
        }
      }
      first = false;
      String sizeLine = readLine();
      if (sizeLine == null) {
        throw new IOException("chunked body truncated");
      }
      int semi = sizeLine.indexOf(';');
      String hex = (semi < 0 ? sizeLine : sizeLine.substring(0, semi)).trim();
      if (hex.isEmpty()) {
        throw new IOException("empty chunk size");
      }
      long size = 0L;
      for (int i = 0; i < hex.length(); i++) {
        int d = hex(hex.charAt(i));
        if (d < 0) {
          throw new IOException("bad chunk size: " + hex);
        }
        size = (size << 4) | d;
      }
      if (size == 0L) {
        // consume optional trailers
        String t = readLine();
        while (t != null && !t.isEmpty()) {
          t = readLine();
        }
        done = true;
        remaining = 0L;
        return;
      }
      remaining = size;
    }

    @Override
    public int read() throws IOException {
      byte[] one = new byte[1];
      int n = read(one, 0, 1);
      return n < 0 ? -1 : (one[0] & 0xFF);
    }

    @Override
    public int read(byte[] b, int off, int len) throws IOException {
      if (done) {
        return -1;
      }
      if (remaining == 0L) {
        nextChunk();
        if (done) {
          return -1;
        }
      }
      int want = (int) Math.min((long) len, remaining);
      int n = in.read(b, off, want);
      if (n < 0) {
        throw new IOException("chunked body truncated");
      }
      remaining -= n;
      return n;
    }

    @Override
    public void close() {
      // underlying socket is closed by the caller
    }
  }

  /** Parses just a chunk-size line (used by the unit test). */
  public static long parseChunkSize(String sizeLine) {
    if (sizeLine == null) {
      return -1L;
    }
    int semi = sizeLine.indexOf(';');
    String hex = (semi < 0 ? sizeLine : sizeLine.substring(0, semi)).trim();
    if (hex.isEmpty()) {
      return -1L;
    }
    long size = 0L;
    for (int i = 0; i < hex.length(); i++) {
      int d = hex(hex.charAt(i));
      if (d < 0) {
        return -1L;
      }
      size = (size << 4) | d;
    }
    return size;
  }

  // ---------------------------------------------------------------- file names

  /**
   * Reduces an arbitrary client-supplied name to a safe leaf file name.
   *
   * @return a non-empty name; never {@code null}.
   */
  public static String safeFileName(String raw) {
    String s = raw == null ? "" : raw;
    int slash = Math.max(s.lastIndexOf('/'), s.lastIndexOf('\\'));
    if (slash >= 0) {
      s = s.substring(slash + 1);
    }
    StringBuilder sb = new StringBuilder(s.length());
    for (int i = 0; i < s.length(); i++) {
      char c = s.charAt(i);
      if (c < 0x20 || c == 0x7F) {
        continue;
      }
      if ("\\/:*?\"<>|".indexOf(c) >= 0) {
        sb.append('_');
        continue;
      }
      sb.append(c);
    }
    String name = sb.toString().trim();
    while (name.startsWith(".")) {
      name = name.substring(1);
    }
    while (name.endsWith(".") || name.endsWith(" ")) {
      name = name.substring(0, name.length() - 1);
    }
    if (name.isEmpty()) {
      return "download";
    }
    if (name.length() > MAX_FILE_NAME) {
      int dot = name.lastIndexOf('.');
      String ext = (dot > 0 && name.length() - dot <= 16) ? name.substring(dot) : "";
      int keep = MAX_FILE_NAME - ext.length();
      name = name.substring(0, Math.max(1, keep)) + ext;
    }
    return name;
  }

  /** Inserts {@code (n)} before the extension: {@code a.txt} + 2 -> {@code a (2).txt}. */
  public static String uniquify(String name, int n) {
    if (n <= 0) {
      return name;
    }
    int dot = name.lastIndexOf('.');
    if (dot <= 0) {
      return name + " (" + n + ")";
    }
    return name.substring(0, dot) + " (" + n + ")" + name.substring(dot);
  }

  /** Minimal JSON string escaping (no android import, so hand-rolled). */
  public static String jsonEscape(String s) {
    if (s == null) {
      return "";
    }
    StringBuilder sb = new StringBuilder(s.length() + 16);
    for (int i = 0; i < s.length(); i++) {
      char c = s.charAt(i);
      switch (c) {
        case '"':
          sb.append("\\\"");
          break;
        case '\\':
          sb.append("\\\\");
          break;
        case '\n':
          sb.append("\\n");
          break;
        case '\r':
          sb.append("\\r");
          break;
        case '\t':
          sb.append("\\t");
          break;
        default:
          if (c < 0x20) {
            sb.append(String.format("\\u%04x", (int) c));
          } else {
            sb.append(c);
          }
      }
    }
    return sb.toString();
  }
}
