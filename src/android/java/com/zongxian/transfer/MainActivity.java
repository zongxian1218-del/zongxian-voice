package com.zongxian.transfer;

import android.Manifest;
import android.app.Activity;
import android.app.DownloadManager;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.util.Log;
import android.view.KeyEvent;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.CookieManager;
import android.webkit.DownloadListener;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.FrameLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;

/**
 * Single-activity WebView shell for 棕仙的传输软件.
 *
 * <p>页面优先从本机回环 HTTP 服务加载（origin 是 http://127.0.0.1:port，属于"安全上下文"，
 * crypto.subtle 才可用）。但只要这一步在任何机型上不成立——系统代理/VPN 把 127.0.0.1 也代理走、
 * 端口起不来、WebView 版本异常——都必须**自动退回内存加载**并把原因显示在屏幕上，
 * 绝不能留给用户一个黑屏。
 */
public class MainActivity extends Activity {

  private static final String TAG = "ZongxianMain";
  private static final int REQ_WRITE_STORAGE = 1001;
  private static final int REQ_FILE_CHOOSER = 1002;
  private static final String MEM_BASE = "https://appassets.zongxian/";

  private WebView web;
  private LocalHttpServer server;
  private FrameLayout root;
  private TextView status;
  private boolean usedMemoryFallback;
  private String pendingUrl;
  /** 网页里 <input type="file"> 的回调，必须由我们弹系统选择器再回填。 */
  private android.webkit.ValueCallback<Uri[]> filePathCallback;

  @Override
  protected void onCreate(Bundle savedInstanceState) {
    super.onCreate(savedInstanceState);

    root = new FrameLayout(this);
    root.setBackgroundColor(Color.parseColor("#1f1f1f"));
    setContentView(root);

    // 先把状态文字放上去：后面任何一步出问题，屏幕上都有话说
    status = new TextView(this);
    status.setTextColor(Color.parseColor("#c8c8c8"));
    status.setTextSize(14f);
    status.setPadding(dp(20), dp(56), dp(20), dp(20));
    status.setText("棕仙的传输软件 正在启动…\n\n正在启动本地服务（127.0.0.1）…");
    root.addView(status, new FrameLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));

    try {
      startEverything();
    } catch (Throwable t) {
      Log.e(TAG, "startup failed", t);
      showFatal("启动失败：" + t + "\n\n" + stack(t));
    }
  }

  private void startEverything() {
    // API 24-28 only: writing to the public Downloads folder needs the runtime grant.
    try {
      if (Build.VERSION.SDK_INT < Build.VERSION_CODES.Q
          && checkSelfPermission(Manifest.permission.WRITE_EXTERNAL_STORAGE)
          != PackageManager.PERMISSION_GRANTED) {
        requestPermissions(new String[]{Manifest.permission.WRITE_EXTERNAL_STORAGE},
            REQ_WRITE_STORAGE);
      }
    } catch (Throwable t) {
      Log.w(TAG, "storage permission request failed: " + t);
    }

    // WebView 本身不可用（少数定制系统把系统 WebView 停用了）时，给出人话提示而不是闪退
    try {
      web = new WebView(this);
    } catch (Throwable t) {
      Log.e(TAG, "WebView unavailable", t);
      showFatal("这台手机的系统 WebView 不可用，无法显示界面。\n\n"
          + "请在应用商店安装/启用「Android System WebView」后重开本应用。\n\n" + stack(t));
      return;
    }
    web.setBackgroundColor(Color.parseColor("#1f1f1f"));
    configureWebView(web);

    int port = -1;
    try {
      server = new LocalHttpServer(getAssets(), new DownloadSaver(this));
      port = server.start();
      Log.i(TAG, "local server ok, port=" + port);
    } catch (Throwable t) {
      Log.e(TAG, "local server failed", t);
      server = null;
    }

    try {
      web.addJavascriptInterface(new NativeBridge(this, server), "ZongxianNative");
    } catch (Throwable t) {
      Log.w(TAG, "bridge failed: " + t);
    }

    if (port > 0) {
      pendingUrl = "http://127.0.0.1:" + port + "/index.html";
      status.setText("本地服务已就绪（127.0.0.1:" + port + "），正在加载界面…");
      Log.i(TAG, "loading " + pendingUrl);
      web.loadUrl(pendingUrl);
    } else {
      loadMemoryPage("本地服务没有启动成功");
    }

    // 页面盖在状态文字之上；页面加载成功后状态层隐藏
    root.addView(web, new FrameLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
    status.bringToFront();
  }

  /** 完全不依赖网络的加载方式：直接把 assets 里的单文件页面塞进 WebView。 */
  private void loadMemoryPage(String why) {
    if (usedMemoryFallback) {
      showFatal("界面加载失败，且内存模式也没成功。\n\n原因：" + why
          + "\n\n请把这段信息截图发给开发者。");
      return;
    }
    usedMemoryFallback = true;
    Log.w(TAG, "loading in-memory page, reason: " + why);
    status.setText("正在以兼容模式加载界面…\n\n（" + why + "）");
    try {
      String html = readAsset("web/index.html");
      if (web == null) {
        // 极端情况：WebView 还没建起来，重新建一个再试
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#1f1f1f"));
        configureWebView(web);
        root.addView(web, new FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        try {
          web.addJavascriptInterface(new NativeBridge(this, server), "ZongxianNative");
        } catch (Throwable t) {
          Log.w(TAG, "bridge failed: " + t);
        }
      }
      web.loadDataWithBaseURL(MEM_BASE, html, "text/html", "utf-8", null);
      Toast.makeText(this, "已用兼容模式打开（本机服务不可用，接收的文件可能无法自动保存）",
          Toast.LENGTH_LONG).show();
    } catch (Throwable t) {
      Log.e(TAG, "in-memory load failed", t);
      showFatal("界面加载失败：" + t + "\n\n" + stack(t));
    }
  }

  private void configureWebView(WebView view) {
    WebSettings s = view.getSettings();
    s.setJavaScriptEnabled(true);
    s.setDomStorageEnabled(true);
    s.setDatabaseEnabled(true);
    s.setMediaPlaybackRequiresUserGesture(false);
    s.setLoadWithOverviewMode(true);
    s.setUseWideViewPort(true);
    s.setSupportZoom(false);
    s.setBuiltInZoomControls(false);
    s.setAllowFileAccess(false);
    s.setAllowContentAccess(false);
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.JELLY_BEAN) {
      s.setAllowFileAccessFromFileURLs(false);
      s.setAllowUniversalAccessFromFileURLs(false);
    }
    // 页面从 http://127.0.0.1 或 https://appassets… 出发去连公网 wss:// 信令
    if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.LOLLIPOP) {
      s.setMixedContentMode(WebSettings.MIXED_CONTENT_COMPATIBILITY_MODE);
    }
    try {
      CookieManager.getInstance().setAcceptThirdPartyCookies(view, false);
    } catch (Throwable ignored) {
      // nothing to do
    }

    view.setWebChromeClient(new WebChromeClient() {
      @Override
      public void onPermissionRequest(final android.webkit.PermissionRequest request) {
        runOnUiThread(new Runnable() {
          @Override
          public void run() {
            request.deny();
          }
        });
      }

      /**
       * 关键：网页里的 &lt;input type="file"&gt;（"选择文件"按钮）在 WebView 里
       * 必须由宿主实现这个回调去弹系统的文件选择器，否则**点了完全没反应**。
       */
      @Override
      public boolean onShowFileChooser(WebView v, android.webkit.ValueCallback<Uri[]> callback,
                                       FileChooserParams params) {
        if (filePathCallback != null) {
          try {
            filePathCallback.onReceiveValue(null);
          } catch (Throwable ignored) {
            // nothing to do
          }
        }
        filePathCallback = callback;
        try {
          Intent intent = new Intent(Intent.ACTION_GET_CONTENT);
          intent.addCategory(Intent.CATEGORY_OPENABLE);
          intent.setType("*/*");
          boolean multi = params != null
              && params.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE;
          if (multi) {
            intent.putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true);
          }
          startActivityForResult(
              Intent.createChooser(intent, multi ? "选择要发送的文件（可多选）" : "选择要发送的文件"),
              REQ_FILE_CHOOSER);
          return true;
        } catch (Throwable t) {
          Log.e(TAG, "file chooser failed", t);
          filePathCallback = null;
          Toast.makeText(MainActivity.this, "打不开文件选择器：" + t, Toast.LENGTH_LONG).show();
          return false;
        }
      }
    });

    // 关键：没有 WebViewClient 的话，页面加载失败只会留一个黑屏给用户。
    view.setWebViewClient(new WebViewClient() {
      @Override
      public void onPageFinished(WebView v, String url) {
        Log.i(TAG, "page finished: " + url);
        if (status != null) {
          status.setVisibility(View.GONE);
        }
      }

      @Override
      public void onReceivedError(WebView v, WebResourceRequest request, WebResourceError error) {
        boolean main = request == null || request.isForMainFrame();
        String desc = error == null ? "unknown" : (error.getErrorCode() + " " + error.getDescription());
        Log.e(TAG, "page error (main=" + main + "): " + desc);
        if (main && !usedMemoryFallback) {
          // 最常见的元凶：手机上的代理/VPN 把 127.0.0.1 的请求也代理走了
          loadMemoryPage("本地页面加载失败：" + desc);
        }
      }

      @Override
      public void onReceivedHttpError(WebView v, WebResourceRequest request,
                                      WebResourceResponse response) {
        boolean main = request == null || request.isForMainFrame();
        int code = response == null ? -1 : response.getStatusCode();
        Log.e(TAG, "http error (main=" + main + "): " + code);
        if (main && code >= 400 && !usedMemoryFallback) {
          loadMemoryPage("本地服务返回 " + code);
        }
      }

      @Override
      public boolean onRenderProcessGone(WebView v, RenderProcessGoneDetail detail) {
        Log.e(TAG, "render process gone, crashed=" + (detail != null && detail.didCrash()));
        if (status != null) {
          status.setVisibility(View.VISIBLE);
          status.setText("界面进程被系统回收了（常见于内存紧张）。\n\n请退出后重新打开本应用。");
        }
        return true; // 已处理：不让整个应用跟着被杀掉
      }
    });

    view.setDownloadListener(new DownloadListener() {
      @Override
      public void onDownloadStart(String url, String userAgent, String contentDisposition,
                                  String mimeType, long contentLength) {
        if (url == null) {
          return;
        }
        String lower = url.toLowerCase(java.util.Locale.US);
        if (lower.startsWith("blob:") || lower.startsWith("data:")) {
          Log.i(TAG, "ignoring blob:/data: download (handled by /__save)");
          return;
        }
        if (lower.startsWith("http://") || lower.startsWith("https://")) {
          try {
            DownloadManager.Request req = new DownloadManager.Request(Uri.parse(url));
            req.setMimeType(mimeType);
            req.addRequestHeader("User-Agent", userAgent);
            req.setNotificationVisibility(
                DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED);
            req.setDestinationInExternalPublicDir(
                android.os.Environment.DIRECTORY_DOWNLOADS,
                HttpCore.safeFileName(android.webkit.URLUtil.guessFileName(
                    url, contentDisposition, mimeType)));
            DownloadManager dm = (DownloadManager) getSystemService(Context.DOWNLOAD_SERVICE);
            if (dm != null) {
              dm.enqueue(req);
            }
          } catch (RuntimeException e) {
            Log.w(TAG, "DownloadManager enqueue failed: " + e);
            Toast.makeText(MainActivity.this, "下载失败：" + e.getMessage(), Toast.LENGTH_SHORT)
                .show();
          }
        }
      }
    });
  }

  private String readAsset(String path) throws IOException {
    InputStream in = getAssets().open(path);
    try {
      ByteArrayOutputStream out = new ByteArrayOutputStream(Math.max(1024, in.available()));
      byte[] buf = new byte[65536];
      int n;
      while ((n = in.read(buf)) > 0) {
        out.write(buf, 0, n);
      }
      return new String(out.toByteArray(), "UTF-8");
    } finally {
      try {
        in.close();
      } catch (IOException ignored) {
        // nothing to do
      }
    }
  }

  private int dp(int v) {
    return (int) (v * getResources().getDisplayMetrics().density + 0.5f);
  }

  private static String stack(Throwable t) {
    java.io.StringWriter sw = new java.io.StringWriter();
    t.printStackTrace(new java.io.PrintWriter(sw));
    String s = sw.toString();
    return s.length() > 1200 ? s.substring(0, 1200) + "…" : s;
  }

  /** 把错误直接画在屏幕上：用户截图就能反馈，不必连电脑抓 logcat。 */
  private void showFatal(String message) {
    if (root == null) {
      return;
    }
    TextView tv = new TextView(this);
    tv.setText(message);
    tv.setTextColor(Color.parseColor("#e9ecf8"));
    tv.setPadding(dp(20), dp(56), dp(20), dp(20));
    tv.setTextSize(14f);
    tv.setTextIsSelectable(true);
    root.removeAllViews();
    root.addView(tv, new FrameLayout.LayoutParams(
        ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
  }

  @Override
  protected void onActivityResult(int requestCode, int resultCode, Intent data) {
    if (requestCode == REQ_FILE_CHOOSER) {
      if (filePathCallback != null) {
        Uri[] result = null;
        try {
          result = WebChromeClient.FileChooserParams.parseResult(resultCode, data);
        } catch (Throwable t) {
          Log.w(TAG, "parseResult failed: " + t);
        }
        try {
          filePathCallback.onReceiveValue(result);
        } catch (Throwable t) {
          Log.w(TAG, "onReceiveValue failed: " + t);
        }
        filePathCallback = null;
      }
      return;
    }
    super.onActivityResult(requestCode, resultCode, data);
  }

  @Override
  protected void onResume() {
    super.onResume();
    if (web != null) {
      try {
        web.onResume();
      } catch (Throwable ignored) {
        // nothing to do
      }
    }
  }

  @Override
  protected void onPause() {
    if (web != null) {
      try {
        web.onPause();
      } catch (Throwable ignored) {
        // nothing to do
      }
    }
    super.onPause();
  }

  @Override
  public boolean onKeyDown(int keyCode, KeyEvent event) {
    if (keyCode == KeyEvent.KEYCODE_BACK && web != null && web.canGoBack()) {
      web.goBack();
      return true;
    }
    return super.onKeyDown(keyCode, event);
  }

  @Override
  protected void onDestroy() {
    if (server != null) {
      try {
        server.stop();
      } catch (Throwable ignored) {
        // nothing to do
      }
      server = null;
    }
    if (web != null) {
      try {
        root.removeView(web);
        web.destroy();
      } catch (Throwable ignored) {
        // nothing to do
      }
      web = null;
    }
    super.onDestroy();
  }
}
