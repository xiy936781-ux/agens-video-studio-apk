package com.agens.videostudio;

import android.app.Activity;
import android.os.Bundle;
import android.graphics.Color;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.Button;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.FrameLayout;

public class MainActivity extends Activity {
    private static final String LOCAL_URL = "http://127.0.0.1:8090";
    private WebView webView;
    private LinearLayout landing;

    @Override
    public void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        showLanding();
    }

    private void showLanding() {
        landing = new LinearLayout(this);
        landing.setOrientation(LinearLayout.VERTICAL);
        landing.setGravity(Gravity.CENTER);
        landing.setPadding(32, 32, 32, 32);
        landing.setBackgroundColor(Color.rgb(8, 12, 28));

        TextView title = new TextView(this);
        title.setText("AGENS  /  VIDEO STUDIO");
        title.setTextColor(Color.rgb(112, 245, 220));
        title.setTextSize(24);
        title.setGravity(Gravity.CENTER);
        title.setPadding(0, 0, 0, 20);
        landing.addView(title, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        TextView note = new TextView(this);
        note.setText("霓虹 AI 视频工作室\n\n请先在 Termux 启动工作室服务：\ncd ~/termux_ai_video_studio/termux_ai_video_studio && ./start.sh\n\n保持 Termux 服务运行，再点下方连接。");
        note.setTextColor(Color.rgb(220, 228, 245));
        note.setTextSize(16);
        note.setGravity(Gravity.CENTER);
        note.setLineSpacing(5, 1.0f);
        landing.addView(note, new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT));

        Button open = new Button(this);
        open.setText("连接并打开工作室");
        open.setOnClickListener(v -> loadStudio());
        LinearLayout.LayoutParams bp = new LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT);
        bp.topMargin = 28;
        landing.addView(open, bp);

        setContentView(landing);
    }

    private void loadStudio() {
        FrameLayout root = new FrameLayout(this);
        webView = new WebView(this);
        webView.setBackgroundColor(Color.rgb(8, 12, 28));
        webView.getSettings().setJavaScriptEnabled(true);
        webView.getSettings().setDomStorageEnabled(true);
        webView.getSettings().setAllowFileAccess(false);
        webView.getSettings().setMixedContentMode(android.webkit.WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        webView.setWebChromeClient(new WebChromeClient());
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                String url = request.getUrl().toString();
                return !url.startsWith("http://127.0.0.1:8090") && !url.startsWith("http://localhost:8090");
            }
            @Override
            public void onReceivedError(WebView view, WebResourceRequest request,
                    android.webkit.WebResourceError error) {
                if (request.isForMainFrame()) showLanding();
            }
        });
        root.addView(webView, new FrameLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT));
        setContentView(root);
        webView.loadUrl(LOCAL_URL);
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            showLanding();
        }
    }

    @Override
    protected void onDestroy() {
        if (webView != null) {
            webView.stopLoading();
            webView.destroy();
            webView = null;
        }
        super.onDestroy();
    }
}
