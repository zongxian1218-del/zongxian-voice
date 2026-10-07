package com.zongxian.transfer;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.LinkedHashMap;
import java.util.List;

/**
 * Desktop-JVM unit test for the pure HTTP logic in {@link HttpCore}.
 *
 * <p>Compiled and run by {@code build/android/selftest.py} with a plain {@code javac}/{@code java}
 * from the bundled JDK - no Android device, emulator or android.jar involved. It asserts the exact
 * branches {@link LocalHttpServer} relies on: request-line parsing, header parsing, percent
 * decoding (including Chinese names), asset path mapping (traversal rejection), Range parsing,
 * chunked-size parsing, chunked stream decoding, and download file-name sanitation.
 */
public final class HttpCoreTest {

  private static int passed;
  private static int failed;

  private static void check(String what, boolean ok) {
    if (ok) {
      passed++;
      System.out.println("  PASS  " + what);
    } else {
      failed++;
      System.out.println("  FAIL  " + what);
    }
  }

  private static void eq(String what, Object expected, Object actual) {
    boolean ok = expected == null ? actual == null : expected.equals(actual);
    if (ok) {
      passed++;
      System.out.println("  PASS  " + what + "  -> " + actual);
    } else {
      failed++;
      System.out.println("  FAIL  " + what + "  expected=" + expected + " actual=" + actual);
    }
  }

  public static void main(String[] args) throws Exception {
    System.out.println("== parseRequestLine ==");
    HttpCore.RequestLine rl =
        HttpCore.parseRequestLine("GET /index.html HTTP/1.1");
    check("GET /index.html parses", rl != null);
    if (rl != null) {
      eq("method", "GET", rl.method);
      eq("rawPath", "/index.html", rl.rawPath);
      eq("rawQuery", null, rl.rawQuery);
      eq("version", "HTTP/1.1", rl.version);
    }

    rl = HttpCore.parseRequestLine("POST /__save?name=a%20b.txt HTTP/1.1");
    check("POST /__save parses", rl != null);
    if (rl != null) {
      eq("method", "POST", rl.method);
      eq("rawPath", "/__save", rl.rawPath);
      eq("rawQuery", "name=a%20b.txt", rl.rawQuery);
    }

    rl = HttpCore.parseRequestLine("GET http://127.0.0.1:23456/index.html HTTP/1.1");
    check("absolute-form target", rl != null && "/index.html".equals(rl.rawPath));

    rl = HttpCore.parseRequestLine("GET /a/b?x=1&y=2 HTTP/1.0");
    check("query with two params", rl != null && "x=1&y=2".equals(rl.rawQuery));

    check("empty line rejected", HttpCore.parseRequestLine("") == null);
    check("garbage rejected", HttpCore.parseRequestLine("not-a-request") == null);
    check("missing version rejected", HttpCore.parseRequestLine("GET /x") == null);
    check("non-HTTP version rejected", HttpCore.parseRequestLine("GET /x FOO/1.1") == null);
    check("null rejected", HttpCore.parseRequestLine(null) == null);
    check("CRLF trimmed", HttpCore.parseRequestLine("GET /x HTTP/1.1\r\n") != null
        && "/x".equals(HttpCore.parseRequestLine("GET /x HTTP/1.1\r\n").rawPath));

    System.out.println("== parseHeaders / header lookup ==");
    List<String> lines = Arrays.asList(
        "Host: 127.0.0.1:23456",
        "Content-Length: 1048576",
        "Transfer-Encoding: chunked",
        "Expect: 100-continue",
        "X-Folded: part1",
        "\tpart2",
        "range: bytes=0-99");
    LinkedHashMap<String, List<String>> h = HttpCore.parseHeaders(lines);
    eq("case-insensitive Content-Length", "1048576", HttpCore.header(h, "content-length"));
    eq("Host", "127.0.0.1:23456", HttpCore.header(h, "Host"));
    eq("obs-fold joined", "part1 part2", HttpCore.header(h, "x-folded"));
    eq("missing header -> null", null, HttpCore.header(h, "nope"));
    check("headerHasToken chunked", HttpCore.headerHasToken(h, "transfer-encoding", "chunked"));
    check("!headerHasToken gzip", !HttpCore.headerHasToken(h, "transfer-encoding", "gzip"));
    check("headerHasToken list form",
        HttpCore.headerHasToken(HttpCore.parseHeaders(Arrays.asList("TE: gzip, chunked")),
            "te", "chunked"));
    check("malformed header line skipped",
        HttpCore.parseHeaders(Arrays.asList("no-colon-here")).isEmpty());

    System.out.println("== parseDecimal ==");
    eq("1048576", 1048576L, HttpCore.parseDecimal("1048576"));
    eq("spaces tolerated", 42L, HttpCore.parseDecimal("  42 "));
    eq("negative rejected", -1L, HttpCore.parseDecimal("-5"));
    eq("junk rejected", -1L, HttpCore.parseDecimal("12x"));
    eq("null rejected", -1L, HttpCore.parseDecimal(null));
    eq("empty rejected", -1L, HttpCore.parseDecimal(""));

    System.out.println("== urlDecode ==");
    eq("%20 -> space (path)", "/a b.txt", HttpCore.urlDecode("/a%20b.txt", false));
    eq("+ literal in path", "/a+b", HttpCore.urlDecode("/a+b", false));
    eq("+ as space in query", "a b", HttpCore.urlDecode("a+b", true));
    eq("chinese utf-8", "棕仙的传输软件",
        HttpCore.urlDecode("%E6%A3%95%E4%BB%99%E7%9A%84%E4%BC%A0%E8%BE%93%E8%BD%AF%E4%BB%B6",
            false));
    eq("mixed literal + escaped chinese", "报告 2024.pdf",
        HttpCore.urlDecode("%E6%8A%A5%E5%91%8A%202024.pdf", false));
    eq("bad escape rejected", null, HttpCore.urlDecode("/a%zz", false));
    eq("truncated escape rejected", null, HttpCore.urlDecode("/a%4", false));
    eq("raw non-ascii passthrough", "文件.txt", HttpCore.urlDecode("文件.txt", false));

    System.out.println("== queryParam ==");
    eq("name param", "报告.pdf", HttpCore.queryParam("name=%E6%8A%A5%E5%91%8A.pdf", "name"));
    eq("name param with plus", "a b.bin", HttpCore.queryParam("x=1&name=a+b.bin", "name"));
    eq("missing name", null, HttpCore.queryParam("foo=1", "name"));
    eq("empty query", null, HttpCore.queryParam("", "name"));
    eq("null query", null, HttpCore.queryParam(null, "name"));

    System.out.println("== assetPath mapping (traversal rejection) ==");
    eq("/ -> index.html", "index.html", HttpCore.assetPath("/"));
    eq("/index.html", "index.html", HttpCore.assetPath("/index.html"));
    eq("empty -> index.html", "index.html", HttpCore.assetPath(""));
    eq("dir -> index.html", "app/index.html", HttpCore.assetPath("/app/"));
    eq("nested", "a/b/c.js", HttpCore.assetPath("/a/b/c.js"));
    eq("dot-segment collapsed", "a/c.js", HttpCore.assetPath("/a/./c.js"));
    eq("double slash collapsed", "a/b.js", HttpCore.assetPath("//a//b.js"));
    eq("percent-encoded name", "报告.pdf", HttpCore.assetPath("/%E6%8A%A5%E5%91%8A.pdf"));
    eq("REJECT /../etc/passwd", null, HttpCore.assetPath("/../etc/passwd"));
    eq("REJECT /a/../../b", null, HttpCore.assetPath("/a/../../b"));
    eq("REJECT %2e%2e", null, HttpCore.assetPath("/%2e%2e/secret"));
    eq("REJECT encoded slash traversal", null, HttpCore.assetPath("/a/%2e%2e%2fb"));
    eq("REJECT backslash traversal", null, HttpCore.assetPath("/..\\..\\windows"));
    eq("REJECT NUL byte", null, HttpCore.assetPath("/a%00b"));
    eq("REJECT drive-ish prefix", null, HttpCore.assetPath("/C:/windows"));
    eq("REJECT malformed escape", null, HttpCore.assetPath("/a%2"));

    System.out.println("== mimeType ==");
    eq("html", "text/html; charset=utf-8", HttpCore.mimeType("index.html"));
    eq("js", "text/javascript; charset=utf-8", HttpCore.mimeType("a/b/app.js"));
    eq("css", "text/css; charset=utf-8", HttpCore.mimeType("x.css"));
    eq("png", "image/png", HttpCore.mimeType("ic_launcher.png"));
    eq("json", "application/json; charset=utf-8", HttpCore.mimeType("m.json"));
    eq("wasm", "application/wasm", HttpCore.mimeType("m.wasm"));
    eq("unknown -> octet-stream", "application/octet-stream", HttpCore.mimeType("weird.zzz"));
    eq("no extension", "application/octet-stream", HttpCore.mimeType("LICENSE"));
    eq("null safe", "application/octet-stream", HttpCore.mimeType(null));

    System.out.println("== parseRange ==");
    long[] r = HttpCore.parseRange("bytes=0-99", 1000L);
    check("bytes=0-99", r != null && r[0] == 0L && r[1] == 99L);
    r = HttpCore.parseRange("bytes=100-", 1000L);
    check("bytes=100- open ended", r != null && r[0] == 100L && r[1] == 999L);
    r = HttpCore.parseRange("bytes=-200", 1000L);
    check("bytes=-200 suffix", r != null && r[0] == 800L && r[1] == 999L);
    r = HttpCore.parseRange("bytes=0-999999", 1000L);
    check("end clamped to size-1", r != null && r[0] == 0L && r[1] == 999L);
    r = HttpCore.parseRange("bytes=-5000", 1000L);
    check("suffix larger than size -> whole", r != null && r[0] == 0L && r[1] == 999L);
    check("start >= size ignored", HttpCore.parseRange("bytes=1000-1200", 1000L) == null);
    check("multi-range ignored", HttpCore.parseRange("bytes=0-10,20-30", 1000L) == null);
    check("non-bytes unit ignored", HttpCore.parseRange("items=0-10", 1000L) == null);
    check("malformed ignored", HttpCore.parseRange("bytes=abc-def", 1000L) == null);
    check("empty size ignored", HttpCore.parseRange("bytes=0-10", 0L) == null);
    check("null ignored", HttpCore.parseRange(null, 1000L) == null);
    check("case-insensitive unit", HttpCore.parseRange("BYTES=5-9", 100L) != null);

    System.out.println("== chunked transfer decoding ==");
    eq("parseChunkSize '1a'", 26L, HttpCore.parseChunkSize("1a"));
    eq("parseChunkSize with extension", 26L, HttpCore.parseChunkSize("1a;foo=bar"));
    eq("parseChunkSize '0'", 0L, HttpCore.parseChunkSize("0"));
    eq("parseChunkSize junk", -1L, HttpCore.parseChunkSize("zz"));
    eq("parseChunkSize null", -1L, HttpCore.parseChunkSize(null));

    byte[] wire = ("5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n").getBytes("US-ASCII");
    InputStream cin = new HttpCore.ChunkedInputStream(new ByteArrayInputStream(wire));
    byte[] got = readAll(cin);
    eq("chunked body decoded", "hello world", new String(got, "US-ASCII"));

    byte[] trailerWire =
        ("4\r\nabcd\r\n0\r\nX-Checksum: 9\r\n\r\n").getBytes("US-ASCII");
    eq("chunked with trailer", "abcd",
        new String(readAll(new HttpCore.ChunkedInputStream(
            new ByteArrayInputStream(trailerWire))), "US-ASCII"));

    byte[] big = new byte[70000];
    Arrays.fill(big, (byte) 'z');
    byte[] wire2 = ("11170\r\n" + new String(big, "US-ASCII") + "\r\n0\r\n\r\n").getBytes("US-ASCII");
    eq("chunked 70000 bytes", 70000,
        readAll(new HttpCore.ChunkedInputStream(new ByteArrayInputStream(wire2))).length);

    boolean threw = false;
    try {
      readAll(new HttpCore.ChunkedInputStream(
          new ByteArrayInputStream("5\r\nhel".getBytes("US-ASCII"))));
    } catch (IOException e) {
      threw = true;
    }
    check("truncated chunked body throws", threw);

    System.out.println("== safeFileName / uniquify ==");
    eq("plain", "a.txt", HttpCore.safeFileName("a.txt"));
    eq("chinese kept", "报告.pdf", HttpCore.safeFileName("报告.pdf"));
    eq("path stripped", "passwd", HttpCore.safeFileName("../../etc/passwd"));
    eq("windows path stripped", "boot.ini", HttpCore.safeFileName("C:\\windows\\boot.ini"));
    eq("traversal only -> download", "download", HttpCore.safeFileName(".."));
    eq("empty -> download", "download", HttpCore.safeFileName(""));
    eq("null -> download", "download", HttpCore.safeFileName(null));
    eq("leading dots stripped", "bashrc", HttpCore.safeFileName(".bashrc"));
    eq("colon replaced", "a_b.txt", HttpCore.safeFileName("a:b.txt"));
    eq("control chars dropped", "ab.txt", HttpCore.safeFileName("a\u0001b.txt"));
    check("long name truncated", HttpCore.safeFileName(repeat("x", 400) + ".bin").length() <= 180);
    eq("long name keeps extension", ".bin",
        HttpCore.safeFileName(repeat("x", 400) + ".bin")
            .substring(HttpCore.safeFileName(repeat("x", 400) + ".bin").length() - 4));

    eq("uniquify n=0 unchanged", "a.txt", HttpCore.uniquify("a.txt", 0));
    eq("uniquify n=1", "a (1).txt", HttpCore.uniquify("a.txt", 1));
    eq("uniquify n=12", "a (12).txt", HttpCore.uniquify("a.txt", 12));
    eq("uniquify no extension", "a (3)", HttpCore.uniquify("a", 3));
    eq("uniquify chinese", "报告 (2).pdf", HttpCore.uniquify("报告.pdf", 2));
    eq("uniquify dotfile", ".hidden (1)", HttpCore.uniquify(".hidden", 1));

    System.out.println("== jsonEscape ==");
    eq("quote escaped", "a\\\"b", HttpCore.jsonEscape("a\"b"));
    eq("backslash escaped", "a\\\\b", HttpCore.jsonEscape("a\\b"));
    eq("newline escaped", "a\\nb", HttpCore.jsonEscape("a\nb"));
    eq("chinese untouched", "报告.pdf", HttpCore.jsonEscape("报告.pdf"));
    eq("null -> empty", "", HttpCore.jsonEscape(null));

    System.out.println();
    System.out.println("RESULT: " + passed + " passed, " + failed + " failed");
    if (failed > 0) {
      System.exit(1);
    }
  }

  private static String repeat(String s, int n) {
    StringBuilder sb = new StringBuilder(s.length() * n);
    for (int i = 0; i < n; i++) {
      sb.append(s);
    }
    return sb.toString();
  }

  private static byte[] readAll(InputStream in) throws IOException {
    List<byte[]> chunks = new ArrayList<byte[]>();
    int total = 0;
    byte[] buf = new byte[4096];
    int n;
    while ((n = in.read(buf)) > 0) {
      byte[] copy = new byte[n];
      System.arraycopy(buf, 0, copy, 0, n);
      chunks.add(copy);
      total += n;
    }
    byte[] out = new byte[total];
    int off = 0;
    for (byte[] c : chunks) {
      System.arraycopy(c, 0, out, off, c.length);
      off += c.length;
    }
    return out;
  }
}
