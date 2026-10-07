package com.zongxian.transfer;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.media.MediaScannerConnection;
import android.net.Uri;
import android.os.Build;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Log;

import java.io.BufferedOutputStream;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * Streams a received file into the public Downloads location.
 *
 * <ul>
 *   <li>API 29+ : {@link MediaStore.Downloads} with the {@code IS_PENDING} handshake, so the file
 *       shows up in the "Downloads" app without a manual media scan.</li>
 *   <li>API 24-28 : direct write to {@code Environment.getExternalStoragePublicDirectory(
 *       DIRECTORY_DOWNLOADS)} followed by a {@link MediaScannerConnection} scan
 *       (needs the runtime {@code WRITE_EXTERNAL_STORAGE} grant).</li>
 * </ul>
 *
 * <p>Both paths stream the body, never buffering the whole file in memory, and both auto-uniquify
 * a colliding name to {@code name (1).ext}.
 */
public final class DownloadSaver implements LocalHttpServer.SaveSink {

  private static final String TAG = "ZongxianSave";
  private static final int BUF = 1 << 16;

  private final Context context;

  public DownloadSaver(Context context) {
    this.context = context.getApplicationContext();
  }

  @Override
  public String save(String fileName, InputStream body, long contentLength) throws IOException {
    String name = HttpCore.safeFileName(fileName);
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
      return saveViaMediaStore(name, body, contentLength);
    }
    return saveViaLegacyFile(name, body, contentLength);
  }

  // ------------------------------------------------------------------ API 29+

  private String saveViaMediaStore(String name, InputStream body, long contentLength)
      throws IOException {
    ContentResolver cr = context.getContentResolver();
    Uri collection = MediaStore.Downloads.EXTERNAL_CONTENT_URI;
    String unique = uniqueMediaStoreName(cr, collection, name);

    ContentValues values = new ContentValues();
    values.put(MediaStore.MediaColumns.DISPLAY_NAME, unique);
    values.put(MediaStore.MediaColumns.MIME_TYPE, HttpCore.mimeType(unique));
    values.put(MediaStore.MediaColumns.IS_PENDING, 1);

    Uri item = cr.insert(collection, values);
    if (item == null) {
      throw new IOException("MediaStore insert returned null for " + unique);
    }
    OutputStream os = null;
    try {
      os = cr.openOutputStream(item, "w");
      if (os == null) {
        throw new IOException("MediaStore openOutputStream returned null");
      }
      BufferedOutputStream bos = new BufferedOutputStream(os, BUF);
      long written = copy(body, bos, contentLength);
      bos.flush();
      os.close();
      os = null;

      ContentValues done = new ContentValues();
      done.put(MediaStore.MediaColumns.IS_PENDING, 0);
      cr.update(item, done, null, null);

      String path = resolveMediaStorePath(cr, item, unique);
      Log.i(TAG, "MediaStore wrote " + written + " bytes -> " + path);
      return path;
    } catch (IOException e) {
      if (os != null) {
        try {
          os.close();
        } catch (IOException ignored) {
          // nothing to do
        }
      }
      try {
        cr.delete(item, null, null);
      } catch (RuntimeException ignored) {
        // nothing to do
      }
      throw e;
    } catch (RuntimeException e) {
      if (os != null) {
        try {
          os.close();
        } catch (IOException ignored) {
          // nothing to do
        }
      }
      try {
        cr.delete(item, null, null);
      } catch (RuntimeException ignored) {
        // nothing to do
      }
      throw new IOException("MediaStore write failed: " + e, e);
    }
  }

  private static String uniqueMediaStoreName(ContentResolver cr, Uri collection, String name) {
    String candidate = name;
    for (int i = 1; i <= 999; i++) {
      candidate = HttpCore.uniquify(name, i - 1);
      Cursor c = null;
      try {
        c = cr.query(collection, new String[]{MediaStore.MediaColumns._ID},
            MediaStore.MediaColumns.DISPLAY_NAME + "=?",
            new String[]{candidate}, null);
        if (c == null || c.getCount() == 0) {
          return candidate;
        }
      } catch (RuntimeException e) {
        return candidate; // can't probe: let MediaStore arbitrate
      } finally {
        if (c != null) {
          c.close();
        }
      }
    }
    return candidate;
  }

  private static String resolveMediaStorePath(ContentResolver cr, Uri item, String fallbackName) {
    Cursor c = null;
    try {
      c = cr.query(item, new String[]{MediaStore.MediaColumns.DATA}, null, null, null);
      if (c != null && c.moveToFirst()) {
        int idx = c.getColumnIndex(MediaStore.MediaColumns.DATA);
        if (idx >= 0 && !c.isNull(idx)) {
          String data = c.getString(idx);
          if (data != null && !data.isEmpty()) {
            return data;
          }
        }
      }
    } catch (RuntimeException ignored) {
      // DATA is deprecated; fall through to the constructed path
    } finally {
      if (c != null) {
        c.close();
      }
    }
    File dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
    return new File(dir, fallbackName).getAbsolutePath();
  }

  // ------------------------------------------------------------------ API <29

  private String saveViaLegacyFile(String name, InputStream body, long contentLength)
      throws IOException {
    File dir = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOWNLOADS);
    if (!dir.isDirectory() && !dir.mkdirs()) {
      throw new IOException("cannot create " + dir.getAbsolutePath());
    }
    File target = uniqueFile(dir, name);
    FileOutputStream fos = null;
    try {
      fos = new FileOutputStream(target);
      BufferedOutputStream bos = new BufferedOutputStream(fos, BUF);
      long written = copy(body, bos, contentLength);
      bos.flush();
      fos.getFD().sync();
      Log.i(TAG, "legacy wrote " + written + " bytes -> " + target.getAbsolutePath());
    } finally {
      if (fos != null) {
        try {
          fos.close();
        } catch (IOException ignored) {
          // nothing to do
        }
      }
    }
    try {
      MediaScannerConnection.scanFile(context,
          new String[]{target.getAbsolutePath()},
          new String[]{HttpCore.mimeType(name)}, null);
    } catch (RuntimeException e) {
      Log.w(TAG, "media scan failed: " + e);
    }
    return target.getAbsolutePath();
  }

  private static File uniqueFile(File dir, String name) {
    for (int i = 0; i <= 999; i++) {
      File f = new File(dir, HttpCore.uniquify(name, i));
      if (!f.exists()) {
        return f;
      }
    }
    return new File(dir, System.currentTimeMillis() + "-" + name);
  }

  // -------------------------------------------------------------------- copy

  /** Streams exactly {@code contentLength} bytes when known, else to EOF. */
  private static long copy(InputStream in, OutputStream out, long contentLength)
      throws IOException {
    byte[] buf = new byte[BUF];
    long total = 0L;
    if (contentLength >= 0) {
      long left = contentLength;
      while (left > 0) {
        int want = (int) Math.min((long) buf.length, left);
        int n = in.read(buf, 0, want);
        if (n < 0) {
          throw new IOException("request body truncated at " + total + "/" + contentLength);
        }
        out.write(buf, 0, n);
        left -= n;
        total += n;
      }
      return total;
    }
    int n;
    while ((n = in.read(buf)) > 0) {
      out.write(buf, 0, n);
      total += n;
    }
    return total;
  }
}
