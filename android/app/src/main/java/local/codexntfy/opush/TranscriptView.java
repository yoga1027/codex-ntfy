package local.codexntfy.opush;

import android.content.ActivityNotFoundException;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Toast;

import org.json.JSONObject;

import java.io.ByteArrayInputStream;
import java.io.IOException;
import java.util.Collections;
import java.util.function.Consumer;

final class TranscriptView extends WebView {
    private static final String ORIGIN = "https://chat.coding-love.invalid";
    private static final String PAGE = ORIGIN + "/transcript.html";
    private boolean ready;
    private JSONObject pending;

    TranscriptView(Context context, Consumer<Uri> action) {
        super(context);
        setBackgroundColor(android.graphics.Color.TRANSPARENT);
        getSettings().setJavaScriptEnabled(true);
        getSettings().setAllowFileAccess(false);
        getSettings().setAllowContentAccess(false);
        getSettings().setSupportMultipleWindows(false);
        getSettings().setTextZoom(Math.round(getResources().getConfiguration().fontScale * 100));
        setWebViewClient(new WebViewClient() {
            @Override
            public WebResourceResponse shouldInterceptRequest(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (isLocal(uri)) {
                    WebResourceResponse asset = loadAsset(uri.getPath());
                    if (asset != null) return asset;
                }
                return new WebResourceResponse("text/plain", "UTF-8", 404, "Not Found",
                        Collections.emptyMap(), new ByteArrayInputStream(new byte[0]));
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                if (PAGE.equals(url)) {
                    ready = true;
                    if (pending != null) show(pending);
                }
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                if (!request.isForMainFrame() || !request.hasGesture()) return true;
                Uri url = request.getUrl();
                String scheme = url.getScheme();
                if (isLocal(url)) {
                    if (url.getPath() != null && url.getPath().startsWith("/action/")) action.accept(url);
                } else if ("https".equals(scheme) || "http".equals(scheme)) {
                    try {
                        context.startActivity(new Intent(Intent.ACTION_VIEW, url));
                    } catch (ActivityNotFoundException ignored) {
                        Toast.makeText(context, R.string.conversation_link_unavailable, Toast.LENGTH_SHORT).show();
                    }
                }
                return true;
            }
        });
        loadUrl(PAGE);
    }

    void show(JSONObject data) {
        pending = data;
        if (ready) {
            String value = JSONObject.quote(data.toString()).replace("\u2028", "\\u2028").replace("\u2029", "\\u2029");
            evaluateJavascript("renderTranscript(JSON.parse(" + value + "))", null);
        }
    }

    private boolean isLocal(Uri url) {
        return "https".equals(url.getScheme()) && "chat.coding-love.invalid".equals(url.getEncodedAuthority());
    }

    private WebResourceResponse loadAsset(String path) {
        if (path == null || !path.matches("/(?:transcript\\.(?:html|css|js)|vendor/(?:markdown-it\\.min\\.js|"
                + "katex\\.min\\.(?:js|css)|fonts/[A-Za-z0-9_-]+\\.woff2))")) return null;
        String type;
        if (path.endsWith(".html")) type = "text/html";
        else if (path.endsWith(".js")) type = "text/javascript";
        else if (path.endsWith(".css")) type = "text/css";
        else type = "font/woff2";
        try {
            return new WebResourceResponse(type, path.endsWith(".woff2") ? null : "UTF-8",
                    getContext().getAssets().open("chat" + path));
        } catch (IOException ignored) {
            return null;
        }
    }
}
