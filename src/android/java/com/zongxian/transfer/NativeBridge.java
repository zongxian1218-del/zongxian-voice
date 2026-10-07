package com.zongxian.transfer;

import android.app.Activity;
import android.webkit.JavascriptInterface;
import android.widget.Toast;

/**
 * The {@code window.ZongxianNative} bridge.
 *
 * <p>Contract with the web page (names and signatures are frozen):
 * <pre>
 *   ZongxianNative.saveUrl() -> "http://127.0.0.1:PORT/__save"
 *   ZongxianNative.toast(s)  -> Android Toast
 *   ZongxianNative.version() -> "1.0"
 * </pre>
 */
public final class NativeBridge {

  private final Activity activity;
  private final LocalHttpServer server;

  public NativeBridge(Activity activity, LocalHttpServer server) {
    this.activity = activity;
    this.server = server;
  }

  /** Absolute URL the page must POST received file bodies to; empty when unavailable. */
  @JavascriptInterface
  public String saveUrl() {
    if (server == null) {
      return "";
    }
    try {
      String u = server.saveUrl();
      return u == null ? "" : u;
    } catch (Throwable t) {
      return "";
    }
  }

  /** True when the page should hand received files to the native saver. */
  @JavascriptInterface
  public boolean canSave() {
    return !saveUrl().isEmpty();
  }

  /** Short UI hint; always marshalled onto the UI thread. */
  @JavascriptInterface
  public void toast(final String message) {
    final String text = message == null ? "" : message;
    activity.runOnUiThread(new Runnable() {
      @Override
      public void run() {
        Toast.makeText(activity, text, Toast.LENGTH_SHORT).show();
      }
    });
  }

  /** Matches {@code versionName} in the manifest. */
  @JavascriptInterface
  public String version() {
    return "1.0";
  }
}
