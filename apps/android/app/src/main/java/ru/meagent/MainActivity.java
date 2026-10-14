package ru.meagent;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.view.KeyEvent;
import android.webkit.CookieManager;
import android.webkit.PermissionRequest;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

/** me for Android. Two modes:
 *  - cloud: the web app (/app); the agent runs in your cloud sandbox.
 *  - phone: the agent runs on this phone in Termux (`me web`), the app shows it from 127.0.0.1. Needs Termux with
 *    allow-external-apps=true in ~/.termux/termux.properties (the app explains how on first use). */
public class MainActivity extends Activity {
    private static final int PICK = 2;
    private static final int LOCAL_PORT = 8765;
    private static final String TERMUX = "com.termux";
    private WebView web;
    private ValueCallback<Uri[]> pending;
    private SharedPreferences prefs;
    private int retries = 0;
    private final Handler ui = new Handler(Looper.getMainLooper());

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        prefs = getSharedPreferences("me", MODE_PRIVATE);
        getWindow().setStatusBarColor(Color.parseColor("#0b0c10"));
        getWindow().setNavigationBarColor(Color.parseColor("#0b0c10"));
        web = new WebView(this);
        web.setBackgroundColor(Color.parseColor("#0b0c10"));
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);
        s.setAllowFileAccess(false);
        CookieManager.getInstance().setAcceptCookie(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView v, WebResourceRequest r) {
                Uri u = r.getUrl();
                String s = u.toString();
                if (s.startsWith(BuildConfig.ME_SERVER) || s.startsWith("http://127.0.0.1:" + LOCAL_PORT)) return false;
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, u));  // external links (t.me, docs, downloads) open outside
                } catch (ActivityNotFoundException e) { /* nothing can open it */ }
                return true;
            }

            @Override
            public void onReceivedError(WebView v, WebResourceRequest r, WebResourceError e) {
                // phone mode: `me web` in Termux needs a few seconds to start; keep trying, then explain
                if (r.isForMainFrame() && "local".equals(mode()) && retries++ < 40) ui.postDelayed(() -> web.loadUrl(localUrl()), 750);
                else if (r.isForMainFrame() && "local".equals(mode())) termuxHelp();
            }
        });
        web.setWebChromeClient(new WebChromeClient() {
            @Override
            public void onPermissionRequest(final PermissionRequest req) {
                if (checkSelfPermission(Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{Manifest.permission.RECORD_AUDIO}, 1);
                }
                runOnUiThread(() -> req.grant(req.getResources()));
            }

            @Override  // the composer's attach button: photos, screenshots and files from the phone
            public boolean onShowFileChooser(WebView v, ValueCallback<Uri[]> cb, FileChooserParams p) {
                if (pending != null) pending.onReceiveValue(null);
                pending = cb;
                Intent i = new Intent(Intent.ACTION_GET_CONTENT).addCategory(Intent.CATEGORY_OPENABLE).setType("*/*")
                        .putExtra(Intent.EXTRA_ALLOW_MULTIPLE, p.getMode() == FileChooserParams.MODE_OPEN_MULTIPLE);
                try {
                    startActivityForResult(Intent.createChooser(i, "Attach"), PICK);
                } catch (ActivityNotFoundException e) {
                    pending = null;
                    return false;
                }
                return true;
            }
        });
        setContentView(web);
        if (state != null) web.restoreState(state);
        else if (mode() == null) chooseMode();
        else open();
    }

    private String mode() { return prefs.getString("mode", null); }

    private String localUrl() { return "http://127.0.0.1:" + LOCAL_PORT + "/"; }

    private boolean hasTermux() {
        try {
            getPackageManager().getPackageInfo(TERMUX, 0);
            return true;
        } catch (PackageManager.NameNotFoundException e) {
            return false;
        }
    }

    private void chooseMode() {
        new AlertDialog.Builder(this).setTitle("Where should the agent run?")
                .setItems(new String[]{"Cloud sandbox (works everywhere)", "On this phone (Termux)"}, (d, which) -> {
                    prefs.edit().putString("mode", which == 0 ? "cloud" : "local").apply();
                    open();
                }).setCancelable(false).show();
    }

    private void open() {
        retries = 0;
        if ("local".equals(mode())) {
            if (!hasTermux()) {
                Toast.makeText(this, "Install Termux (F-Droid) to run the agent on the phone", Toast.LENGTH_LONG).show();
                startActivity(new Intent(Intent.ACTION_VIEW, Uri.parse("https://f-droid.org/packages/com.termux/")));
                prefs.edit().remove("mode").apply();
                return;
            }
            startTermux();
            web.loadUrl(localUrl());
        } else {
            web.loadUrl(BuildConfig.ME_SERVER + "/app");
        }
    }

    /** Ask Termux to install me (first time) and start `me web` in the background. */
    private void startTermux() {
        if (checkSelfPermission("com.termux.permission.RUN_COMMAND") != PackageManager.PERMISSION_GRANTED) {
            requestPermissions(new String[]{"com.termux.permission.RUN_COMMAND"}, 3);
        }
        String script = "export PREFIX=${PREFIX:-/data/data/com.termux/files/usr} TERMUX_VERSION=${TERMUX_VERSION:-app}; "
                + "export PATH=$PREFIX/bin:$PATH; command -v me >/dev/null 2>&1 || (pkg install -y python curl >/dev/null 2>&1; curl -fsSL "
                + BuildConfig.ME_SERVER + "/i | sh); exec \"$PREFIX/bin/me\" web " + LOCAL_PORT + " --no-open";
        Intent i = new Intent().setClassName(TERMUX, "com.termux.app.RunCommandService").setAction("com.termux.RUN_COMMAND")
                .putExtra("com.termux.RUN_COMMAND_PATH", "/data/data/com.termux/files/usr/bin/sh")
                .putExtra("com.termux.RUN_COMMAND_ARGUMENTS", new String[]{"-c", script})
                .putExtra("com.termux.RUN_COMMAND_BACKGROUND", true);
        try {
            startService(i);
        } catch (Exception e) {
            termuxHelp();
        }
    }

    private void termuxHelp() {
        new AlertDialog.Builder(this).setTitle("Allow me to use Termux")
                .setMessage("In Termux run once:\n\necho allow-external-apps=true >> ~/.termux/termux.properties\n\n"
                        + "then reopen this app. Or start it yourself in Termux:  me web " + LOCAL_PORT)
                .setPositiveButton("Retry", (d, w) -> open())
                .setNeutralButton("Use cloud", (d, w) -> { prefs.edit().putString("mode", "cloud").apply(); open(); })
                .show();
    }

    @Override
    protected void onActivityResult(int req, int res, Intent data) {
        super.onActivityResult(req, res, data);
        if (req != PICK || pending == null) return;
        Uri[] out = null;
        if (res == RESULT_OK && data != null) {
            if (data.getClipData() != null) {
                out = new Uri[data.getClipData().getItemCount()];
                for (int k = 0; k < out.length; k++) out[k] = data.getClipData().getItemAt(k).getUri();
            } else if (data.getData() != null) {
                out = new Uri[]{data.getData()};
            }
        }
        pending.onReceiveValue(out);
        pending = null;
    }

    @Override
    public boolean onKeyLongPress(int code, KeyEvent e) {
        if (code == KeyEvent.KEYCODE_BACK) {  // long-press back: switch between cloud and phone mode
            prefs.edit().remove("mode").apply();
            chooseMode();
            return true;
        }
        return super.onKeyLongPress(code, e);
    }

    @Override
    public boolean onKeyDown(int code, KeyEvent e) {
        if (code == KeyEvent.KEYCODE_BACK) e.startTracking();
        return super.onKeyDown(code, e);
    }

    @Override
    protected void onSaveInstanceState(Bundle out) {
        super.onSaveInstanceState(out);
        web.saveState(out);
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
