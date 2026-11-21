package local.codexntfy.opush;

import android.Manifest;
import android.app.Activity;
import android.app.AlertDialog;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.content.Context;
import android.content.ClipData;
import android.content.Intent;
import android.content.SharedPreferences;
import android.content.pm.PackageManager;
import android.content.res.ColorStateList;
import android.graphics.Bitmap;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.graphics.drawable.RippleDrawable;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.provider.Settings;
import android.text.InputType;
import android.text.InputFilter;
import android.text.TextUtils;
import android.view.Gravity;
import android.view.View;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.ImageView;
import android.widget.LinearLayout;
import android.widget.PopupMenu;
import android.widget.ScrollView;
import android.widget.Switch;
import android.widget.TextView;
import android.widget.Toast;

import com.heytap.msp.push.HeytapPushManager;
import com.heytap.msp.push.callback.ICallBackResultService;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.io.File;
import java.text.DateFormat;
import java.time.Instant;
import java.time.LocalDate;
import java.time.ZoneId;
import java.util.ArrayList;
import java.util.Date;
import java.util.HashMap;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.UUID;
import java.util.concurrent.Callable;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.function.Consumer;

public final class MainActivity extends Activity {
    static final String ACTION_OPEN_CONNECTION = "local.codexntfy.opush.OPEN_CONNECTION";
    static final String ACTION_OPEN_REMINDER = "local.codexntfy.opush.OPEN_REMINDER";

    private static final String OPPO_CHANNEL = "codex_opush_test";
    private static final int REQUEST_NOTIFICATIONS = 1;
    private static final int REQUEST_PHOTO = 10;
    private static final int REPLY_CAMERA = 30;
    private static final int REPLY_IMAGE = 31;
    private static final int REPLY_FILE = 32;
    private static final int MATCH = ViewGroup.LayoutParams.MATCH_PARENT;
    private static final int WRAP = ViewGroup.LayoutParams.WRAP_CONTENT;
    private static final Handler MAIN = new Handler(Looper.getMainLooper());
    private static final Typeface MEDIUM = Typeface.create("sans-serif-medium", Typeface.NORMAL);
    private static boolean registering;

    private enum Page { INBOX, PROJECTS, PROJECT_THREADS, CONNECTION, DETAIL, OPPO }

    private final SharedPreferences.OnSharedPreferenceChangeListener registrationChanged =
            (prefs, key) -> updateOppo();
    private final SharedPreferences.OnSharedPreferenceChangeListener receiverChanged =
            (prefs, key) -> updateConnection();
    private final SharedPreferences.OnSharedPreferenceChangeListener historyChanged =
            (prefs, key) -> {
                if (ReminderHistory.KEY.equals(key)) {
                    updateHistory();
                    if (this.page == Page.INBOX && this.scroll != null) {
                        this.scroll.smoothScrollTo(0, 0);
                    }
                }
            };
    private final SharedPreferences.OnSharedPreferenceChangeListener catalogChanged =
            (prefs, key) -> {
                if (isProjectPage() && ThreadStore.PROJECTS.equals(key)) {
                    updateProjects();
                } else if (ThreadStore.CATALOG.equals(key) || ThreadStore.PROJECTS.equals(key)
                        || (key != null && key.startsWith("preview:"))) {
                    updateHistory();
                }
            };
    private final ExecutorService catalogWorker = Executors.newSingleThreadExecutor();
    private final ExecutorService historyWorker = Executors.newSingleThreadExecutor();
    private final ExecutorService settingsWorker = Executors.newSingleThreadExecutor();
    private Future<?> projectHistorySync;
    private final Runnable refreshCatalog = this::readCatalog;
    private final Runnable openTailscaleIfNeeded = this::openTailscaleIfNeeded;
    private final Runnable verifyTailscaleStopped = this::verifyTailscaleStopped;

    private NotificationManager notifications;
    private SharedPreferences registration;
    private SharedPreferences receiver;
    private SharedPreferences history;
    private ThreadStore threads;
    private ProjectOrganizer organizer;
    private PhotoAppearance appearance;
    private TextView latencyResult;
    private Button latencyButton;
    private boolean latencyMeasuring;
    private Long latencyMs;
    private boolean latencyFailed;
    private Page page = Page.INBOX;
    private Page detailBack = Page.INBOX;
    private String selectedProject = "";
    private String selectedProjectName = "";
    private String reminderId;
    private String attachmentTarget;
    private String cameraFile;
    private String threadId;
    private ScrollView scroll;
    private ConversationPane conversation;
    private boolean resumed;
    private int inboxScroll;
    private boolean startAfterPermission;
    private LinearLayout content;
    private LinearLayout historyContent;
    private TextView toolbarLabel;
    private TextView toolbarProject;
    private View threadOptions;
    private boolean threadArchived;
    private boolean threadActionRunning;
    private boolean catalogLoading;
    private boolean creatingThread;
    private boolean projectSyncFailed;
    private TextView receiverTitle;
    private TextView receiverDetail;
    private View receiverSignal;
    private Button receiverAction;
    private TextView permissionStatus;
    private EditText appKey;
    private EditText appSecret;
    private TextView oppoStatus;
    private TextView registrationId;
    private Button registerButton;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        registration = getSharedPreferences("opush", MODE_PRIVATE);
        receiver = getSharedPreferences(TailscaleReminderService.PREFS, MODE_PRIVATE);
        history = getSharedPreferences(ReminderHistory.PREFS, MODE_PRIVATE);
        threads = new ThreadStore(this);
        organizer = new ProjectOrganizer(this);
        appearance = new PhotoAppearance(this);
        notifications = getSystemService(NotificationManager.class);
        NotificationChannel channel = new NotificationChannel(
                OPPO_CHANNEL, getString(R.string.oppo_push), NotificationManager.IMPORTANCE_HIGH);
        channel.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        notifications.createNotificationChannel(channel);
        TailscaleReminderService.createChannels(notifications);

        if (state == null) {
            readDestination(getIntent());
        } else {
            page = Page.valueOf(state.getString("page", Page.INBOX.name()));
            detailBack = Page.valueOf(state.getString("detail_back", Page.INBOX.name()));
            selectedProject = state.getString("selected_project", "");
            selectedProjectName = state.getString("selected_project_name", "");
            reminderId = state.getString("reminder_id");
            attachmentTarget = state.getString("attachment_target");
            cameraFile = state.getString("camera_file");
            threadId = state.getString("thread_id", ThreadStore.threadFromEvent(reminderId));
            inboxScroll = state.getInt("inbox_scroll");
            startAfterPermission = state.getBoolean("start_after_permission");
        }
        render();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        rememberInboxScroll();
        String previous = threadId;
        readDestination(intent);
        if (conversation == null || page != Page.DETAIL
                || threadId == null || !Objects.equals(previous, threadId)) {
            render();
        }
    }

    private void readDestination(Intent intent) {
        if (ACTION_OPEN_REMINDER.equals(intent.getAction()) && intent.getData() != null) {
            detailBack = Page.INBOX;
            reminderId = intent.getData().getSchemeSpecificPart();
            threadId = ThreadStore.threadFromEvent(reminderId);
            page = Page.DETAIL;
        } else if (ACTION_OPEN_CONNECTION.equals(intent.getAction())) {
            page = Page.CONNECTION;
        }
    }

    @Override
    protected void onSaveInstanceState(Bundle state) {
        rememberInboxScroll();
        state.putString("page", page.name());
        state.putString("detail_back", detailBack.name());
        state.putString("selected_project", selectedProject);
        state.putString("selected_project_name", selectedProjectName);
        state.putString("reminder_id", reminderId);
        state.putString("attachment_target", attachmentTarget);
        state.putString("camera_file", cameraFile);
        state.putString("thread_id", threadId);
        state.putInt("inbox_scroll", inboxScroll);
        state.putBoolean("start_after_permission", startAfterPermission);
        super.onSaveInstanceState(state);
    }

    @Override
    protected void onResume() {
        super.onResume();
        resumed = true;
        registration.registerOnSharedPreferenceChangeListener(registrationChanged);
        receiver.registerOnSharedPreferenceChangeListener(receiverChanged);
        history.registerOnSharedPreferenceChangeListener(historyChanged);
        threads.preferences.registerOnSharedPreferenceChangeListener(catalogChanged);
        updateConnection();
        updateHistory();
        updateProjects();
        updateOppo();
        if (conversation != null) {
            conversation.resume();
        }
        scheduleCatalog(0);
    }

    @Override
    protected void onPause() {
        resumed = false;
        MAIN.removeCallbacks(refreshCatalog);
        stopProjectHistorySync();
        if (conversation != null) {
            conversation.pause();
        }
        registration.unregisterOnSharedPreferenceChangeListener(registrationChanged);
        receiver.unregisterOnSharedPreferenceChangeListener(receiverChanged);
        history.unregisterOnSharedPreferenceChangeListener(historyChanged);
        threads.preferences.unregisterOnSharedPreferenceChangeListener(catalogChanged);
        super.onPause();
    }

    @Override
    protected void onDestroy() {
        closeConversation();
        MAIN.removeCallbacks(openTailscaleIfNeeded);
        MAIN.removeCallbacks(verifyTailscaleStopped);
        catalogWorker.shutdownNow();
        historyWorker.shutdownNow();
        settingsWorker.shutdownNow();
        super.onDestroy();
    }

    private void closeConversation() {
        if (conversation != null) {
            conversation.close();
            conversation = null;
        }
    }

    @Override
    @SuppressWarnings("deprecation")
    public void onBackPressed() {
        switch (page) {
            case INBOX:
                super.onBackPressed();
                break;
            case DETAIL:
                navigate(detailBack);
                break;
            case PROJECT_THREADS:
                navigate(Page.PROJECTS);
                break;
            case OPPO:
                navigate(Page.CONNECTION);
                break;
            default:
                navigate(Page.INBOX);
        }
    }

    private void navigate(Page destination) {
        if (destination != page) {
            rememberInboxScroll();
            page = destination;
            if (!isProjectPage()) {
                stopProjectHistorySync();
            }
            render();
        }
    }

    private void rememberInboxScroll() {
        if (page == Page.INBOX && scroll != null) {
            inboxScroll = scroll.getScrollY();
        }
    }

    private void render() {
        closeConversation();
        receiverAction = null;
        receiverTitle = null;
        receiverSignal = null;
        permissionStatus = null;
        receiverDetail = null;
        latencyResult = null;
        latencyButton = null;
        if (page != Page.OPPO) {
            appKey = null;
            appSecret = null;
            oppoStatus = null;
            registrationId = null;
            registerButton = null;
        }
        LinearLayout root = column();
        boolean claude = page == Page.DETAIL && ThreadStore.isClaude(threadId);
        int canvas = getColor(claude ? R.color.claude_canvas : R.color.canvas);
        root.setBackgroundColor(canvas);
        root.addView(toolbar(), new LinearLayout.LayoutParams(MATCH, WRAP));

        content = column();
        if (page == Page.DETAIL) {
            scroll = null;
            root.addView(content, new LinearLayout.LayoutParams(MATCH, 0, 1f));
        } else {
            scroll = new ScrollView(this);
            scroll.setFillViewport(true);
            content.setPadding(dp(20), dp(10), dp(20), dp(24));
            content.setFocusableInTouchMode(true);
            scroll.addView(content, new FrameLayout.LayoutParams(MATCH, WRAP));
            root.addView(scroll, new LinearLayout.LayoutParams(MATCH, 0, 1f));
        }

        switch (page) {
            case INBOX:
                buildInbox();
                break;
            case CONNECTION:
                buildConnection();
                break;
            case PROJECTS:
            case PROJECT_THREADS:
                break;
            case DETAIL:
                historyContent = content;
                break;
            case OPPO:
                buildOppo();
                break;
        }
        if (page == Page.INBOX || page == Page.CONNECTION || isProjectPage()) {
            root.addView(divider());
            root.addView(navigationBar(), new LinearLayout.LayoutParams(MATCH, WRAP));
        }
        Bitmap wallpaper = page == Page.OPPO ? null : appearance.get(
                page == Page.DETAIL ? PhotoAppearance.Image.CHAT : PhotoAppearance.Image.HOME);
        if (wallpaper == null) {
            setContentView(root);
        } else {
            FrameLayout layers = new FrameLayout(this);
            layers.setBackgroundColor(canvas);
            ImageView background = new ImageView(this);
            background.setImageBitmap(wallpaper);
            background.setScaleType(ImageView.ScaleType.CENTER_CROP);
            background.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
            layers.addView(background, new FrameLayout.LayoutParams(MATCH, MATCH));
            root.setBackgroundColor((canvas & 0xFFFFFF) | (claude ? 0xC0000000 : 0x80000000));
            layers.addView(root, new FrameLayout.LayoutParams(MATCH, MATCH));
            setContentView(layers);
        }
        updateConnection();
        updateHistory();
        updateProjects();
        updateOppo();
        scheduleCatalog(0);
        if (page == Page.INBOX && inboxScroll > 0) {
            ScrollView inbox = scroll;
            inbox.post(() -> inbox.scrollTo(0, inboxScroll));
        }
    }

    private View toolbar() {
        LinearLayout bar = row();
        bar.setPadding(dp(20), dp(8), dp(20), dp(8));
        bar.setMinimumHeight(dp(64));
        int title = isProjectPage() ? R.string.projects
                : (page == Page.CONNECTION ? R.string.connection : R.string.inbox);
        if (page == Page.DETAIL || page == Page.OPPO || page == Page.PROJECT_THREADS) {
            if (page == Page.DETAIL) {
                title = R.string.reminder_detail;
            } else if (page == Page.OPPO) {
                title = R.string.oppo_push_unpublished;
            }
            FrameLayout back = iconTile(R.drawable.ic_back, R.color.ink, 48, 23, 14, 0);
            back.setContentDescription(getString(R.string.back));
            clickable(back, 14, this::onBackPressed);
            bar.addView(back);
        } else {
            ImageView portrait = icon(R.mipmap.ic_coding_love, 0);
            Bitmap selectedAvatar = appearance.get(PhotoAppearance.Image.AVATAR);
            if (selectedAvatar != null) {
                portrait.setImageBitmap(selectedAvatar);
            }
            portrait.setBackground(shape(8, R.color.coding_love_icon_background, 0));
            portrait.setClipToOutline(true);
            portrait.setScaleType(ImageView.ScaleType.CENTER_CROP);
            bar.addView(portrait, new LinearLayout.LayoutParams(dp(36), dp(36)));
        }
        toolbarLabel = text(getString(title), 21, R.color.ink, true);
        toolbarLabel.setMaxLines(1);
        toolbarLabel.setEllipsize(TextUtils.TruncateAt.END);
        LinearLayout.LayoutParams labelLayout = new LinearLayout.LayoutParams(0, WRAP, 1f);
        labelLayout.setMarginStart(dp(12));
        threadOptions = null;
        if (page == Page.DETAIL) {
            LinearLayout labels = column();
            add(labels, toolbarLabel, 0);
            toolbarProject = text("", 13, R.color.ink_secondary, false);
            toolbarProject.setMaxLines(1);
            toolbarProject.setEllipsize(TextUtils.TruncateAt.END);
            toolbarProject.setVisibility(View.GONE);
            add(labels, toolbarProject, 2);
            bar.addView(labels, labelLayout);
            threadOptions = iconTile(R.drawable.ic_more, R.color.ink, 48, 22, 14, 0);
            threadOptions.setContentDescription(getString(R.string.thread_options));
            threadOptions.setVisibility(threadId == null ? View.GONE : View.VISIBLE);
            clickable(threadOptions, 14, this::showThreadOptions);
            bar.addView(threadOptions);
        } else {
            toolbarProject = null;
            bar.addView(toolbarLabel, labelLayout);
        }
        if (page == Page.INBOX || isProjectPage()) {
            bar.addView(connectionBadge(), new LinearLayout.LayoutParams(WRAP, WRAP));
        }
        return bar;
    }

    private View navigationBar() {
        LinearLayout nav = row();
        nav.setPadding(dp(12), dp(5), dp(12), dp(5));
        nav.setBackgroundColor(getColor(R.color.surface));
        addTab(nav, Page.INBOX, R.string.inbox, R.drawable.ic_inbox);
        addTab(nav, Page.PROJECTS, R.string.projects, R.drawable.ic_projects);
        addTab(nav, Page.CONNECTION, R.string.connection, R.drawable.ic_connection);
        return nav;
    }

    private void addTab(LinearLayout nav, Page target, int title, int glyph) {
        boolean selected = page == target || (target == Page.PROJECTS && isProjectPage());
        int tone = selected ? R.color.accent : R.color.ink_secondary;
        LinearLayout tab = column();
        tab.setGravity(Gravity.CENTER);
        tab.setMinimumHeight(dp(58));
        tab.setPadding(dp(6), dp(4), dp(6), dp(4));
        FrameLayout marker = new FrameLayout(this);
        if (selected) {
            marker.setBackground(shape(16, R.color.surface_tint, 0));
        }
        marker.addView(icon(glyph, tone),
                new FrameLayout.LayoutParams(dp(22), dp(22), Gravity.CENTER));
        tab.addView(marker, new LinearLayout.LayoutParams(dp(62), dp(30)));
        TextView label = text(getString(title), 12, tone, selected);
        label.setGravity(Gravity.CENTER);
        tab.addView(label);
        tab.setContentDescription(getString(title));
        tab.setSelected(selected);
        clickable(tab, 16, () -> navigate(target));
        nav.addView(tab, new LinearLayout.LayoutParams(0, WRAP, 1f));
    }

    private View connectionBadge() {
        LinearLayout status = row();
        status.setPadding(dp(9), dp(4), dp(6), dp(4));
        status.setMinimumHeight(dp(36));
        status.setBackground(shape(18, R.color.surface_tint, 0));
        receiverSignal = new View(this);
        receiverSignal.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        status.addView(receiverSignal, new LinearLayout.LayoutParams(dp(7), dp(7)));
        receiverTitle = text("", 12, R.color.accent, true);
        receiverTitle.setMaxLines(1);
        LinearLayout.LayoutParams title = new LinearLayout.LayoutParams(WRAP, WRAP);
        title.setMarginStart(dp(5));
        status.addView(receiverTitle, title);
        status.addView(icon(R.drawable.ic_chevron, R.color.accent),
                new LinearLayout.LayoutParams(dp(14), dp(14)));
        clickable(status, 18, () -> {
            navigate(Page.CONNECTION);
            startComputerConnection();
        });
        return status;
    }

    private void buildInbox() {
        historyContent = column();
        add(content, historyContent, 0);
    }

    private void buildConnection() {
        add(content, connectionCard(), 0);

        LinearLayout latency = card();
        add(latency, heading(R.string.latency_title, 16), 0);
        latencyResult = text("", 14, R.color.ink_secondary, false);
        add(latency, latencyResult, 4);
        latencyButton = button(R.string.latency_test, this::testLatency);
        styleButton(latencyButton, R.string.latency_test, false);
        add(latency, latencyButton, 9);
        add(content, latency, 12);
        updateLatency();

        add(content, settingsRow(R.drawable.ic_codex, R.string.account_usage,
                text(getString(R.string.account_usage_summary), 13, R.color.ink_secondary, false),
                this::showAccountUsage), 12);

        add(content, heading(R.string.more_settings, 20), 24);
        permissionStatus = text("", 13, R.color.ink_secondary, false);
        add(content, settingsRow(R.drawable.ic_bell, R.string.notification_settings, permissionStatus,
                () -> openNotificationSettings(TailscaleReminderService.REMINDER_CHANNEL, false)), 12);
        add(content, settingsRow(R.drawable.ic_connection, R.string.oppo_push_unpublished,
                text(getString(R.string.oppo_summary), 13, R.color.ink_secondary, false),
                () -> navigate(Page.OPPO)), 8);
        Switch archive = new Switch(this);
        archive.setText(R.string.hide_archived_threads);
        archive.setTextSize(15);
        archive.setTextColor(getColor(R.color.ink));
        archive.setPadding(dp(14), dp(12), dp(14), dp(12));
        archive.setMinHeight(dp(52));
        archive.setBackground(shape(18, R.color.surface, R.color.line));
        archive.setChecked(threads.hideArchived());
        archive.setOnCheckedChangeListener((view, checked) ->
                threads.preferences.edit().putBoolean(ThreadStore.HIDE_ARCHIVED, checked).apply());
        add(content, archive, 8);
        add(content, heading(R.string.appearance, 20), 24);
        for (PhotoAppearance.Image image : PhotoAppearance.Image.values()) {
            boolean custom = appearance.get(image) != null;
            add(content, settingsRow(R.drawable.ic_projects, image.label,
                    text(getString(custom ? R.string.appearance_replace : R.string.appearance_import),
                            13, R.color.ink_secondary, false),
                    () -> choosePhoto(image)), 8);
        }
    }

    private void showAccountUsage() {
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle(R.string.account_usage)
                .setMessage(R.string.account_usage_loading)
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.account_usage_refresh, null).show();
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener(view -> refreshAccountUsage(dialog));
        refreshAccountUsage(dialog);
    }

    private void refreshAccountUsage(AlertDialog dialog) {
        dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(false);
        dialog.setMessage(getString(R.string.account_usage_loading));
        settingsWorker.execute(() -> {
            String message;
            try {
                JSONObject result = new ConversationClient(this).accountUsage();
                message = result == null ? getString(R.string.account_usage_failed) : AccountUsage.format(result);
            } catch (IOException | JSONException | SecurityException ignored) {
                message = getString(R.string.account_usage_failed);
            }
            String display = message;
            MAIN.post(() -> {
                if (isDestroyed() || !dialog.isShowing()) return;
                dialog.setMessage(display);
                dialog.getButton(AlertDialog.BUTTON_POSITIVE).setEnabled(true);
            });
        });
    }

    private void testLatency() {
        if (latencyMeasuring) {
            return;
        }
        latencyMeasuring = true;
        updateLatency();
        settingsWorker.execute(() -> {
            Long measured = null;
            try {
                measured = new ConversationClient(this).measureLatency();
            } catch (IOException | SecurityException ignored) {
                // A failed probe has its own status in the same card.
            }
            Long result = measured;
            MAIN.post(() -> {
                latencyMs = result;
                latencyFailed = result == null;
                latencyMeasuring = false;
                if (!isDestroyed()) {
                    updateLatency();
                }
            });
        });
    }

    private void updateLatency() {
        if (latencyResult == null) {
            return;
        }
        latencyButton.setEnabled(!latencyMeasuring);
        if (latencyMeasuring) {
            latencyResult.setText(R.string.latency_testing);
        } else if (latencyFailed) {
            latencyResult.setText(R.string.latency_failed);
        } else if (latencyMs != null) {
            latencyResult.setText(getString(R.string.latency_result, latencyMs));
        } else {
            latencyResult.setText(R.string.latency_idle);
        }
    }

    private void choosePhoto(PhotoAppearance.Image image) {
        if (appearance.get(image) == null) {
            launchPhotoPicker(image);
            return;
        }
        new AlertDialog.Builder(this).setTitle(image.label)
                .setItems(new CharSequence[]{getString(R.string.appearance_replace),
                        getString(R.string.appearance_reset)}, (dialog, index) -> {
                    if (index == 0) {
                        launchPhotoPicker(image);
                    } else {
                        appearance.reset(image);
                        render();
                    }
                }).setNegativeButton(android.R.string.cancel, null).show();
    }

    @SuppressWarnings("deprecation")
    private void launchPhotoPicker(PhotoAppearance.Image image) {
        Intent picker = new Intent(Build.VERSION.SDK_INT >= 33
                ? MediaStore.ACTION_PICK_IMAGES : Intent.ACTION_OPEN_DOCUMENT).setType("image/*");
        try {
            startActivityForResult(picker, REQUEST_PHOTO + image.ordinal());
        } catch (android.content.ActivityNotFoundException ignored) {
            try {
                startActivityForResult(new Intent(Intent.ACTION_OPEN_DOCUMENT).setType("image/*")
                        .addCategory(Intent.CATEGORY_OPENABLE), REQUEST_PHOTO + image.ordinal());
            } catch (android.content.ActivityNotFoundException unavailable) {
                showThreadMessage(R.string.appearance_import_failed);
            }
        }
    }

    @SuppressWarnings("deprecation")
    void pickReplyAttachment(String target, int action) {
        if (attachmentTarget != null) return;
        attachmentTarget = target;
        try {
            if (action == 0) {
                cameraFile = UUID.randomUUID() + ".jpg";
                File photo = CameraOutput.file(this, cameraFile);
                File directory = photo.getParentFile();
                if ((!directory.isDirectory() && !directory.mkdirs()) || !photo.createNewFile()) {
                    throw new IOException("Cannot create camera file");
                }
                Uri output = CameraOutput.uri(this, cameraFile);
                Intent camera = new Intent(MediaStore.ACTION_IMAGE_CAPTURE).putExtra(MediaStore.EXTRA_OUTPUT, output);
                camera.setClipData(ClipData.newRawUri("photo", output));
                camera.addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION | Intent.FLAG_GRANT_WRITE_URI_PERMISSION);
                startActivityForResult(camera, REPLY_CAMERA);
            } else {
                Intent picker = new Intent(Intent.ACTION_OPEN_DOCUMENT).addCategory(Intent.CATEGORY_OPENABLE)
                        .setType(action == 1 ? "image/*" : "*/*");
                if (action == 1 && Build.VERSION.SDK_INT >= 33) {
                    try {
                        startActivityForResult(new Intent(MediaStore.ACTION_PICK_IMAGES).setType("image/*"), REPLY_IMAGE);
                        return;
                    } catch (android.content.ActivityNotFoundException ignored) {
                        // An Android device may provide the Documents picker without the Photo Picker.
                    }
                }
                startActivityForResult(picker, action == 1 ? REPLY_IMAGE : REPLY_FILE);
            }
        } catch (IOException | RuntimeException error) {
            if (cameraFile != null) CameraOutput.file(this, cameraFile).delete();
            cameraFile = null;
            attachmentTarget = null;
            showThreadMessage(R.string.conversation_attachment_failed);
        }
    }

    @Override
    @SuppressWarnings("deprecation")
    protected void onActivityResult(int request, int result, Intent data) {
        super.onActivityResult(request, result, data);
        if (request == REPLY_CAMERA || request == REPLY_IMAGE || request == REPLY_FILE) {
            String photo = cameraFile;
            String target = attachmentTarget;
            cameraFile = null;
            attachmentTarget = null;
            Uri uri = photo != null ? CameraOutput.uri(this, photo) : (data == null ? null : data.getData());
            if (result == RESULT_OK && uri != null && conversation != null && conversation.matchesAttachmentTarget(target)) {
                conversation.importAttachment(uri, request != REPLY_FILE, photo);
            } else if (photo != null) {
                CameraOutput.file(this, photo).delete();
            }
            return;
        }
        int index = request - REQUEST_PHOTO;
        PhotoAppearance.Image[] images = PhotoAppearance.Image.values();
        if (index < 0 || index >= images.length || result != RESULT_OK || data == null || data.getData() == null) {
            return;
        }
        settingsWorker.execute(() -> {
            try {
                appearance.importPhoto(images[index], data.getData());
                MAIN.post(() -> {
                    if (!isDestroyed()) {
                        render();
                    }
                });
            } catch (IOException | RuntimeException error) {
                MAIN.post(() -> {
                    if (!isDestroyed()) {
                        showThreadMessage(R.string.appearance_import_failed);
                    }
                });
            }
        });
    }

    private View connectionCard() {
        LinearLayout hero = column();
        hero.setPadding(dp(18), dp(16), dp(18), dp(18));
        hero.setBackground(shape(24, R.color.hero, 0));
        LinearLayout top = row();
        top.addView(iconTile(R.drawable.ic_connection, R.color.hero_text, 36, 23, 11, R.color.hero_icon));
        TextView label = text(getString(R.string.tailscale), 14, R.color.hero_text, true);
        LinearLayout.LayoutParams labelLayout = new LinearLayout.LayoutParams(0, WRAP, 1f);
        labelLayout.setMarginStart(dp(10));
        top.addView(label, labelLayout);
        receiverSignal = new View(this);
        receiverSignal.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        top.addView(receiverSignal, new LinearLayout.LayoutParams(dp(9), dp(9)));
        add(hero, top, 0);
        receiverTitle = text("", 24, R.color.hero_text, true);
        receiverDetail = text("", 14, R.color.hero_muted, false);
        add(hero, receiverTitle, 13);
        add(hero, receiverDetail, 4);
        if (!TailscaleReminderService.HOST.isEmpty()) {
            TextView endpoint = text(getString(R.string.receiver_endpoint) + "  " + TailscaleReminderService.HOST,
                    14, R.color.hero_text, false);
            endpoint.setTextIsSelectable(true);
            add(hero, endpoint, 12);
        }
        receiverAction = button(R.string.start_receiver, this::toggleReceiver);
        add(hero, receiverAction, 10);
        clickable(hero, 24, this::startComputerConnection);
        return hero;
    }

    private void updateConnection() {
        if (receiverTitle == null || receiverSignal == null) {
            return;
        }
        TailscaleReminderService.Status status = TailscaleReminderService.status();
        boolean canPost = TailscaleReminderService.canPost(notifications);
        boolean running = status != TailscaleReminderService.Status.STOPPED;
        int title;
        int detail;
        int signal = R.color.signal_wait;
        if (TailscaleReminderService.HOST.isEmpty()) {
            title = R.string.receiver_unconfigured;
            detail = R.string.receiver_unconfigured_detail;
            signal = R.color.signal_off;
        } else if (!canPost) {
            title = R.string.receiver_blocked;
            detail = R.string.notifications_disabled;
        } else {
            switch (status) {
                case CONNECTED:
                    title = R.string.receiver_connected;
                    detail = R.string.receiver_connected_detail;
                    signal = R.color.signal_on;
                    break;
                case WAITING_FOR_VPN:
                    title = R.string.receiver_vpn;
                    detail = R.string.receiver_vpn_detail;
                    break;
                case RETRYING:
                    title = R.string.receiver_retry;
                    detail = R.string.receiver_retry_detail;
                    break;
                case STOPPED:
                    title = R.string.receiver_stopped;
                    detail = R.string.receiver_stopped_detail;
                    signal = R.color.signal_off;
                    break;
                default:
                    title = R.string.receiver_connecting;
                    detail = R.string.receiver_connecting_detail;
            }
        }
        receiverTitle.setText(title);
        if (receiverDetail != null) {
            receiverDetail.setText(detail);
        } else {
            signal = status == TailscaleReminderService.Status.CONNECTED
                    ? R.color.accent : R.color.ink_secondary;
        }
        receiverSignal.setBackground(shape(10, signal, 0));
        if (receiverAction != null) {
            styleButton(receiverAction, running ? R.string.stop_receiver : R.string.start_receiver, !running);
            permissionStatus.setText(canPost ? R.string.notifications_enabled : R.string.notifications_disabled);
        }
    }

    private void toggleReceiver() {
        if (TailscaleReminderService.status() != TailscaleReminderService.Status.STOPPED) {
            MAIN.removeCallbacks(openTailscaleIfNeeded);
            stopService(new Intent(this, TailscaleReminderService.class));
            MAIN.removeCallbacks(verifyTailscaleStopped);
            if (TailscaleConnector.requestDisconnection(this)) {
                MAIN.postDelayed(verifyTailscaleStopped, 8000);
            } else {
                verifyTailscaleStopped();
            }
        } else {
            startComputerConnection();
        }
    }

    private void startComputerConnection() {
        if (TailscaleReminderService.HOST.isEmpty()) {
            Toast.makeText(this, R.string.receiver_unconfigured_detail, Toast.LENGTH_LONG).show();
            return;
        }
        MAIN.removeCallbacks(verifyTailscaleStopped);
        if (!TailscaleReminderService.canPost(notifications)) {
            openNotificationSettings(TailscaleReminderService.REMINDER_CHANNEL, true);
            return;
        }
        boolean starting = TailscaleReminderService.status() == TailscaleReminderService.Status.STOPPED;
        if (starting) {
            startForegroundService(new Intent(this, TailscaleReminderService.class));
        }
        boolean vpnConnected = TailscaleReminderService.tailscaleNetwork(this) != null;
        if (!starting && vpnConnected) {
            return;
        }
        MAIN.removeCallbacks(openTailscaleIfNeeded);
        if (TailscaleConnector.requestConnection(this)) {
            if (!vpnConnected) {
                MAIN.postDelayed(openTailscaleIfNeeded, 8000);
            }
        } else if (!vpnConnected) {
            openTailscaleIfNeeded();
        }
    }

    private void verifyTailscaleStopped() {
        if (!isDestroyed() && TailscaleReminderService.tailscaleNetwork(this) != null) {
            Toast.makeText(this, R.string.tailscale_still_connected, Toast.LENGTH_LONG).show();
        }
    }

    private void openTailscaleIfNeeded() {
        if (resumed && page == Page.CONNECTION && !isDestroyed()
                && TailscaleReminderService.tailscaleNetwork(this) == null
                && !TailscaleConnector.openApp(this)) {
            Toast.makeText(this, R.string.tailscale_unavailable, Toast.LENGTH_SHORT).show();
        }
    }

    private void openNotificationSettings(String channelId, boolean startOnGrant) {
        if (Build.VERSION.SDK_INT >= 33
                && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                        != PackageManager.PERMISSION_GRANTED
                && !getPreferences(MODE_PRIVATE).getBoolean("asked_notifications", false)) {
            startAfterPermission = startOnGrant;
            getPreferences(MODE_PRIVATE).edit().putBoolean("asked_notifications", true).apply();
            requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, REQUEST_NOTIFICATIONS);
            return;
        }
        String action = notifications.areNotificationsEnabled()
                ? Settings.ACTION_CHANNEL_NOTIFICATION_SETTINGS : Settings.ACTION_APP_NOTIFICATION_SETTINGS;
        startActivity(new Intent(action)
                .putExtra(Settings.EXTRA_APP_PACKAGE, getPackageName())
                .putExtra(Settings.EXTRA_CHANNEL_ID, channelId));
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == REQUEST_NOTIFICATIONS) {
            if (startAfterPermission && TailscaleReminderService.canPost(notifications)) {
                startComputerConnection();
            }
            startAfterPermission = false;
            updateConnection();
            updateOppo();
        }
    }

    private void scheduleCatalog(long delay) {
        MAIN.removeCallbacks(refreshCatalog);
        if (resumed && showsCatalog() && !isDestroyed()) {
            MAIN.postDelayed(refreshCatalog, delay);
        }
    }

    private void readCatalog() {
        if (catalogLoading || !resumed || !showsCatalog()) {
            return;
        }
        catalogLoading = true;
        boolean projectCatalog = usesProjectCatalog();
        ConversationClient client = new ConversationClient(this);
        catalogWorker.execute(() -> {
            boolean success = false;
            try {
                if (projectCatalog) {
                    JSONObject response = client.projects(threads.projects().revision);
                    if (response == null) {
                        throw new IOException("Project catalog unavailable");
                    }
                    threads.saveProjects(response);
                } else {
                    threads.saveCatalog(client.catalog());
                }
                success = true;
            } catch (IOException | JSONException | SecurityException ignored) {
                // Keep the cached list while the phone or computer is offline.
            } finally {
                boolean connected = success;
                MAIN.post(() -> {
                    catalogLoading = false;
                    if (projectCatalog && isProjectPage()) {
                        boolean failed = !connected;
                        if (projectSyncFailed != failed) {
                            projectSyncFailed = failed;
                            updateProjects();
                        }
                        if (connected) {
                            syncProjectHistories();
                        }
                    }
                    scheduleCatalog(projectCatalog == usesProjectCatalog() ? 12000 : 0);
                });
            }
        });
    }

    private boolean showsCatalog() {
        return page == Page.INBOX || isProjectPage() || (page == Page.DETAIL && threadId != null);
    }

    private boolean isProjectPage() {
        return page == Page.PROJECTS || page == Page.PROJECT_THREADS;
    }

    private boolean usesProjectCatalog() {
        return isProjectPage() || (page == Page.DETAIL && threadId != null);
    }

    private void syncProjectHistories() {
        if (!resumed || isDestroyed() || (projectHistorySync != null && !projectHistorySync.isDone())) {
            return;
        }
        projectHistorySync = historyWorker.submit(() -> {
            ConversationClient client = new ConversationClient(this);
            for (ThreadStore.Entry entry : threads.projects().threads) {
                if (Thread.currentThread().isInterrupted()) {
                    return;
                }
                if (entry.historyRevision.isEmpty() || threads.hasRevision(entry)) {
                    continue;
                }
                try {
                    JSONObject disk = threads.read(entry.id);
                    JSONObject result = client.readThread(entry.id, disk == null ? "" : disk.optString("revision"));
                    if (result != null && !result.optBoolean("unchanged")) {
                        threads.save(entry.id, result);
                    } else if (result != null && disk != null) {
                        threads.recordRevision(entry.id,
                                result.optString("cache_revision", disk.optString("revision")));
                    }
                } catch (IOException | JSONException | SecurityException ignored) {
                    // Retry any missing histories after the next catalog refresh.
                }
            }
        });
    }

    private void stopProjectHistorySync() {
        if (projectHistorySync != null) {
            projectHistorySync.cancel(true);
            projectHistorySync = null;
        }
    }

    private void updateProjects() {
        if (!isProjectPage() || content == null) {
            return;
        }
        ThreadStore.ProjectCatalog catalog = threads.projects();
        int position = scroll.getScrollY();
        content.removeAllViews();
        if (projectSyncFailed) {
            add(content, text(getString(R.string.projects_offline), 13, R.color.ink_secondary, false), 0);
        }
        if (page == Page.PROJECTS) {
            Map<String, Integer> counts = new HashMap<>();
            for (ThreadStore.Entry entry : catalog.threads) {
                if (!threads.hideArchived() || !entry.archived) {
                    counts.merge(entry.projectId, 1, Integer::sum);
                }
            }
            if (catalog.revision.isEmpty() && !projectSyncFailed) {
                add(content, text(getString(R.string.projects_loading), 14, R.color.ink_secondary, false), 8);
            }
            Map<String, String> projects = new LinkedHashMap<>();
            catalog.names.forEach((id, name) -> projects.put(id, organizer.projectName(id, name)));
            projects.put("", organizer.projectName("", getString(R.string.unassigned_project)));
            for (ProjectOrganizer.Section section : organizer.sections(ProjectOrganizer.PROJECTS, projects, java.util.Collections.emptyMap())) {
                addOrganizationHeading(ProjectOrganizer.PROJECTS, section);
                for (String id : section.ids) {
                    addProjectRow(id, projects.get(id), counts.getOrDefault(id, 0), section);
                }
            }
        } else {
            selectedProjectName = organizer.projectName(selectedProject,
                    catalog.names.getOrDefault(selectedProject, selectedProjectName));
            toolbarLabel.setText(selectedProjectName);
            Button create = button(creatingThread ? R.string.creating_conversation : R.string.new_conversation,
                    this::createConversation);
            create.setEnabled(!creatingThread);
            add(content, create, 8);
            addOrganizedThreads(catalog.threads);
        }
        if (position > 0) {
            ScrollView projectScroll = scroll;
            projectScroll.post(() -> projectScroll.scrollTo(0, position));
        }
    }

    private void addOrganizedThreads(List<ThreadStore.Entry> entries) {
        String scope = ProjectOrganizer.conversations(selectedProject);
        Map<String, ThreadStore.Entry> visible = new LinkedHashMap<>();
        for (ThreadStore.Entry entry : entries) {
            if (selectedProject.equals(entry.projectId) && (!threads.hideArchived() || !entry.archived)) {
                visible.put(entry.id, entry);
            }
        }
        if (visible.isEmpty()) {
            LinearLayout empty = card();
            add(empty, text(getString(R.string.project_empty), 15, R.color.ink_secondary, false), 0);
            add(content, empty, 12);
            return;
        }
        DateFormat clock = android.text.format.DateFormat.getTimeFormat(this);
        Map<String, Long> activity = new LinkedHashMap<>();
        Map<String, String> labels = new LinkedHashMap<>();
        for (ThreadStore.Entry entry : visible.values()) {
            activity.put(entry.id, entry.activityAt);
            labels.put(entry.id, entry.title);
        }
        for (ProjectOrganizer.Section section : organizer.sections(scope, labels, activity)) {
            addOrganizationHeading(scope, section);
            LinearLayout rows = card();
            for (String id : section.ids) {
                if (rows.getChildCount() > 0) {
                    add(rows, divider(), 0);
                }
                View entry = threadRow(visible.get(id), clock);
                organizer.bindItem(entry, scope, id, section, this::updateProjects, null);
                add(rows, entry, 0);
            }
            add(content, rows, 8);
        }
    }

    private void addOrganizationHeading(String scope, ProjectOrganizer.Section section) {
        String label = section.title.replaceAll("[\\p{Z}\\s]+", " ").trim();
        if (!label.isEmpty()) {
            TextView title = text(label, 15, R.color.ink_secondary, true);
            title.setIncludeFontPadding(false);
            title.setPadding(dp(4), dp(2), dp(4), dp(3));
            organizer.bindSection(title, scope, section, this::updateProjects);
            add(content, title, content.getChildCount() == 0 ? 0 : 10);
        }
    }

    private void addProjectRow(String id, String name, int count, ProjectOrganizer.Section section) {
        boolean claudeProject = id.startsWith("claude-project-");
        int logo = claudeProject ? R.drawable.provider_claude : R.drawable.provider_codex;
        View project = settingsRow(logo, name,
                text(getString(R.string.project_thread_count, count), 13, R.color.ink_secondary, false),
                () -> {
                    selectedProject = id;
                    selectedProjectName = name;
                    navigate(Page.PROJECT_THREADS);
                }, 0, claudeProject ? R.color.claude_row : R.color.surface);
        organizer.bindItem(project, ProjectOrganizer.PROJECTS, id, section, this::updateProjects,
                () -> renameProject(id, name));
        add(content, project, 8);
    }

    private void renameProject(String id, String current) {
        EditText field = input(R.string.organization_project_name, InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        field.setFilters(new InputFilter[]{new InputFilter.LengthFilter(80)});
        field.setText(current);
        field.selectAll();
        LinearLayout panel = column();
        panel.setPadding(dp(24), dp(12), dp(24), 0);
        add(panel, field, 0);
        AlertDialog dialog = new AlertDialog.Builder(this).setTitle(R.string.organization_rename_project)
                .setView(panel).setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(android.R.string.ok, null).create();
        dialog.show();
        dialog.getButton(android.content.DialogInterface.BUTTON_POSITIVE).setOnClickListener(view -> {
            String name = field.getText().toString().trim().replaceAll("\\s+", " ");
            if (name.isEmpty()) {
                field.setError(getString(R.string.organization_project_name));
                return;
            }
            dialog.dismiss();
            if (id.isEmpty() || id.startsWith("claude-project-")) {
                organizer.renameLocalProject(id, name);
                updateProjects();
            } else {
                runThreadAction(() -> new ConversationClient(this).renameProject(id, name), result -> {
                    if ("renamed".equals(responseStatus(result))) {
                        threads.renameProject(id, result.optString("name", name));
                        updateProjects();
                        scheduleCatalog(0);
                    } else {
                        showThreadMessage(R.string.organization_project_rename_failed);
                    }
                });
            }
        });
    }

    private void createConversation() {
        if (creatingThread) {
            return;
        }
        String project = selectedProject;
        String key = "create:" + project;
        String requestId = threads.preferences.getString(key, UUID.randomUUID().toString());
        if (!threads.preferences.edit().putString(key, requestId).commit()) {
            Toast.makeText(this, R.string.create_conversation_failed, Toast.LENGTH_SHORT).show();
            return;
        }
        creatingThread = true;
        updateProjects();
        catalogWorker.execute(() -> {
            String id = null;
            int failure = R.string.create_conversation_failed;
            try {
                JSONObject result = new ConversationClient(this).create(project, requestId);
                if ("created".equals(result.optString("status"))) {
                    id = result.optString("thread_id");
                } else if ("project_unavailable".equals(result.optString("status"))) {
                    failure = R.string.project_directory_unavailable;
                }
            } catch (IOException | JSONException | SecurityException ignored) {
                // Reuse this request ID if the response was lost.
            }
            String created = id;
            int error = failure;
            MAIN.post(() -> {
                creatingThread = false;
                if (isDestroyed()) {
                    return;
                }
                if (ThreadStore.validId(created)) {
                    threads.preferences.edit().remove(key).apply();
                    if (page == Page.PROJECT_THREADS && project.equals(selectedProject)) {
                        openThread(created);
                    }
                    scheduleCatalog(0);
                } else {
                    updateProjects();
                    Toast.makeText(this, error, Toast.LENGTH_LONG).show();
                }
            });
        });
    }

    private void updateHistory() {
        if (page != Page.INBOX && page != Page.DETAIL) {
            return;
        }
        List<ReminderHistory.Entry> reminders = ReminderHistory.read(
                history.getString(ReminderHistory.KEY, null));
        List<ThreadStore.Entry> entries = threads.entries(reminders);
        if (page == Page.DETAIL) {
            ReminderHistory.Entry legacy = null;
            if (threadId == null) {
                for (ReminderHistory.Entry entry : reminders) {
                    if (entry.id.equals(reminderId)) {
                        legacy = entry;
                        break;
                    }
                }
            }
            if (threadId != null || legacy != null) {
                ThreadStore.Entry active = usesProjectCatalog() ? threads.projects().find(threadId) : null;
                if (active == null) {
                    for (ThreadStore.Entry entry : entries) {
                        if (entry.id.equals(threadId)) {
                            active = entry;
                            break;
                        }
                    }
                }
                if (active != null) {
                    setThreadTitle(active.title, organizer.projectName(active.projectId, active.project));
                } else if (detailBack == Page.PROJECT_THREADS) {
                    setThreadTitle(getString(R.string.new_conversation), selectedProjectName);
                }
                if (conversation == null) {
                    historyContent.removeAllViews();
                    conversation = new ConversationPane(this, threadId, legacy, this::forkConversation);
                    historyContent.addView(conversation, new LinearLayout.LayoutParams(MATCH, MATCH));
                    if (resumed) {
                        conversation.resume();
                    }
                }
                threadArchived = active != null && active.archived;
                conversation.setArchived(threadArchived);
                return;
            }
            historyContent.removeAllViews();
            TextView missing = text(getString(R.string.reminder_unavailable), 16, R.color.ink_secondary, false);
            missing.setPadding(dp(20), dp(8), dp(20), 0);
            add(historyContent, missing, 0);
            return;
        }
        historyContent.removeAllViews();
        if (threads.hideArchived()) {
            entries.removeIf(entry -> entry.archived);
        }
        if (entries.isEmpty()) {
            showEmptyInbox();
            return;
        }
        LinearLayout rows = column();
        rows.setBackground(shape(16, R.color.surface, R.color.line));
        DateFormat clock = android.text.format.DateFormat.getTimeFormat(this);
        for (int i = 0; i < entries.size(); i++) {
            if (i > 0) {
                View line = divider();
                LinearLayout.LayoutParams dividerLayout = new LinearLayout.LayoutParams(MATCH, dp(1));
                dividerLayout.setMarginStart(dp(14));
                dividerLayout.setMarginEnd(dp(14));
                rows.addView(line, dividerLayout);
            }
            add(rows, threadRow(entries.get(i), clock), 0);
        }
        add(historyContent, rows, 0);
    }

    private void setThreadTitle(String title, String project) {
        toolbarLabel.setText(title);
        toolbarProject.setText(getString(R.string.conversation_project, project));
        toolbarProject.setVisibility(project.isEmpty() ? View.GONE : View.VISIBLE);
    }

    private void showEmptyInbox() {
        LinearLayout empty = card();
        empty.setGravity(Gravity.CENTER_HORIZONTAL);
        empty.setPadding(dp(24), dp(26), dp(24), dp(26));
        empty.addView(iconTile(R.drawable.ic_inbox, R.color.accent, 58, 30, 19, R.color.surface_tint));
        TextView title = text(getString(R.string.inbox_empty), 18, R.color.ink, true);
        title.setGravity(Gravity.CENTER);
        add(empty, title, 13);
        TextView detail = text(getString(R.string.inbox_empty_detail), 14, R.color.ink_secondary, false);
        detail.setGravity(Gravity.CENTER);
        add(empty, detail, 5);
        add(historyContent, empty, 0);
    }

    private View threadRow(ThreadStore.Entry entry, DateFormat clock) {
        LinearLayout box = column();
        if (ThreadStore.isClaude(entry.id)) {
            box.setBackground(shape(14, R.color.claude_row, 0));
        }
        box.setPadding(dp(14), dp(12), dp(14), dp(12));
        box.setMinimumHeight(dp(62));
        LinearLayout top = row();
        TextView name = text(entry.title, 16, R.color.ink, true);
        name.setMaxLines(1);
        name.setEllipsize(TextUtils.TruncateAt.END);
        LinearLayout.LayoutParams titleLayout = new LinearLayout.LayoutParams(0, WRAP, 1f);
        titleLayout.setMarginEnd(dp(8));
        top.addView(name, titleLayout);
        if (entry.archived) {
            TextView archive = text(getString(R.string.archived_thread), 11, R.color.ink_secondary, false);
            archive.setPadding(0, 0, dp(7), 0);
            top.addView(archive);
        }
        if (entry.activityAt > 0) {
            LocalDate day = date(entry.activityAt);
            String label = day.equals(LocalDate.now())
                    ? clock.format(new Date(entry.activityAt)) : dayLabel(day);
            top.addView(text(label, 12, R.color.ink_secondary, false));
        }
        add(box, top, 0);
        String preview = entry.preview.replaceAll("\\s+", " ").trim();
        if (!preview.isEmpty()) {
            TextView summary = text(preview, 13, R.color.ink_secondary, false);
            summary.setMaxLines(1);
            summary.setEllipsize(TextUtils.TruncateAt.END);
            add(box, summary, 4);
        }
        clickable(box, 14, () -> openThread(entry.id));
        return box;
    }

    private void openThread(String id) {
        detailBack = page == Page.PROJECT_THREADS ? Page.PROJECT_THREADS : Page.INBOX;
        threadId = id;
        reminderId = null;
        navigate(Page.DETAIL);
    }

    private void showThreadOptions() {
        if (threadId == null || threadActionRunning) {
            return;
        }
        String id = threadId;
        boolean archive = !threadArchived;
        PopupMenu menu = new PopupMenu(this, threadOptions, Gravity.END);
        ThreadStore.Entry entry = threads.projects().find(id);
        if (entry == null) {
            for (ThreadStore.Entry candidate : threads.entries(ReminderHistory.read(
                    history.getString(ReminderHistory.KEY, "[]")))) {
                if (id.equals(candidate.id)) {
                    entry = candidate;
                    break;
                }
            }
        }
        String project = detailBack == Page.PROJECT_THREADS && entry == null ? selectedProject
                : (entry == null ? "" : entry.projectId);
        if (project.isEmpty() && ThreadStore.isClaude(id)) project = "claude-project-unassigned";
        String scope = ProjectOrganizer.conversations(project);
        menu.getMenu().add(0, 4, 0, organizer.pinned(scope, id) ? R.string.organization_unpin : R.string.organization_pin);
        menu.getMenu().add(0, 3, 1, R.string.rename_conversation_menu);
        menu.getMenu().add(0, 2, 2, R.string.move_conversation).setEnabled(!ThreadStore.isClaude(id));
        menu.getMenu().add(0, 1, 3, archive ? R.string.archive_conversation_menu : R.string.restore_conversation);
        menu.setOnMenuItemClickListener(item -> {
            if (item.getItemId() == 4) {
                organizer.togglePin(scope, id, () -> {});
            } else if (item.getItemId() == 1) {
                changeArchive(id, archive);
            } else if (item.getItemId() == 2) {
                chooseProject(id);
            } else {
                renameConversation(id);
            }
            return true;
        });
        menu.show();
    }

    private void renameConversation(String id) {
        EditText title = input(R.string.conversation_name, InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        title.setFilters(new InputFilter[]{new InputFilter.LengthFilter(120)});
        title.setText(toolbarLabel.getText());
        title.selectAll();
        LinearLayout field = column();
        field.setPadding(dp(24), dp(12), dp(24), 0);
        add(field, title, 0);
        new AlertDialog.Builder(this).setTitle(R.string.rename_conversation).setView(field)
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(android.R.string.ok, (dialog, button) -> {
                    String name = title.getText().toString().trim();
                    if (name.isEmpty()) {
                        return;
                    }
                    runThreadAction(() -> new ConversationClient(this).rename(id, name), response -> {
                        if ("renamed".equals(responseStatus(response))) {
                            threads.renameThread(id, response.optString("title", name));
                            if (page == Page.DETAIL && id.equals(threadId)) {
                                toolbarLabel.setText(response.optString("title", name));
                            }
                            scheduleCatalog(0);
                        } else {
                            showThreadMessage(R.string.rename_conversation_failed);
                        }
                    });
                }).show();
    }

    private void changeArchive(String id, boolean archive) {
        runThreadAction(() -> new ConversationClient(this).setArchive(id, archive), result -> {
            String status = responseStatus(result);
            if ((archive ? "archived" : "active").equals(status)) {
                threads.updateThread(id, archive, null, null);
                if (page == Page.DETAIL && id.equals(threadId)) {
                    threadArchived = archive;
                    conversation.setArchived(archive);
                }
                scheduleCatalog(0);
            } else {
                showThreadMessage("busy".equals(status) ? R.string.conversation_busy
                        : R.string.archive_conversation_failed);
            }
        });
    }

    private void chooseProject(String id) {
        ThreadStore.ProjectCatalog projects = threads.projects();
        if (projects.revision.isEmpty()) {
            runThreadAction(() -> new ConversationClient(this).projects(""), result -> {
                if (result == null) {
                    showThreadMessage(R.string.move_conversation_failed);
                } else if (page == Page.DETAIL && id.equals(threadId)) {
                    threads.saveProjects(result);
                    showProjectPicker(id, threads.projects());
                }
            });
        } else {
            showProjectPicker(id, projects);
        }
    }

    private void showProjectPicker(String id, ThreadStore.ProjectCatalog projects) {
        List<String> ids = new ArrayList<>();
        List<String> titles = new ArrayList<>();
        ids.add("");
        titles.add(getString(R.string.classify_by_directory));
        for (Map.Entry<String, String> project : projects.names.entrySet()) {
            if (project.getKey().startsWith("claude-project-")) {
                continue;
            }
            ids.add(project.getKey());
            titles.add(project.getValue());
        }
        new AlertDialog.Builder(this).setTitle(R.string.move_conversation)
                .setItems(titles.toArray(new String[0]), (dialog, index) -> moveConversation(id, ids.get(index)))
                .setNegativeButton(android.R.string.cancel, null).show();
    }

    private void moveConversation(String id, String project) {
        runThreadAction(() -> new ConversationClient(this).move(id, project), result -> {
            if ("moved".equals(responseStatus(result))) {
                String projectId = result.optString("project_id");
                String name = result.optString("project");
                threads.updateThread(id, null, projectId, name);
                if (page == Page.DETAIL && id.equals(threadId) && detailBack == Page.PROJECT_THREADS) {
                    selectedProject = projectId;
                    selectedProjectName = name;
                }
                scheduleCatalog(0);
            } else {
                showThreadMessage(R.string.move_conversation_failed);
            }
        });
    }

    private void forkConversation(String turn) {
        if (threadId == null || threadActionRunning) {
            return;
        }
        String parent = threadId;
        String key = "fork:" + parent + ":" + turn;
        String request = threads.preferences.getString(key, UUID.randomUUID().toString());
        runThreadAction(() -> {
            if (!threads.preferences.edit().putString(key, request).commit()) {
                return null;
            }
            return new ConversationClient(this).fork(parent, turn, request);
        }, result -> {
            String status = responseStatus(result);
            String created = "created".equals(status) ? result.optString("thread_id") : null;
            if (ThreadStore.validId(created)) {
                threads.preferences.edit().remove(key).apply();
                if (page == Page.DETAIL && parent.equals(threadId)) {
                    selectedProject = result.optString("project_id");
                    selectedProjectName = result.optString("project");
                    threadId = created;
                    reminderId = null;
                    render();
                }
                showThreadMessage(R.string.fork_conversation_opened);
                scheduleCatalog(0);
            } else {
                showThreadMessage("unknown".equals(status) ? R.string.fork_conversation_unknown
                        : R.string.fork_conversation_failed);
            }
        });
    }

    private void runThreadAction(Callable<JSONObject> operation, Consumer<JSONObject> deliver) {
        if (threadActionRunning || isDestroyed()) {
            return;
        }
        threadActionRunning = true;
        if (threadOptions != null) {
            threadOptions.setEnabled(false);
        }
        catalogWorker.execute(() -> {
            JSONObject result = null;
            try {
                result = operation.call();
            } catch (Exception ignored) {
                // The existing local view remains available when the computer cannot be reached.
            }
            JSONObject response = result;
            MAIN.post(() -> {
                threadActionRunning = false;
                if (!isDestroyed()) {
                    if (threadOptions != null) {
                        threadOptions.setEnabled(true);
                    }
                    deliver.accept(response);
                }
            });
        });
    }

    private static String responseStatus(JSONObject result) {
        return result == null ? "" : result.optString("status");
    }

    private void showThreadMessage(int message) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    private static LocalDate date(long time) {
        return Instant.ofEpochMilli(time).atZone(ZoneId.systemDefault()).toLocalDate();
    }

    private String dayLabel(LocalDate day) {
        LocalDate today = LocalDate.now();
        if (today.equals(day)) {
            return getString(R.string.today);
        }
        if (today.minusDays(1).equals(day)) {
            return getString(R.string.yesterday);
        }
        return android.text.format.DateFormat.getDateFormat(this)
                .format(Date.from(day.atStartOfDay(ZoneId.systemDefault()).toInstant()));
    }

    private void buildOppo() {
        LinearLayout card = card();
        card.setImportantForAutofill(View.IMPORTANT_FOR_AUTOFILL_NO_EXCLUDE_DESCENDANTS);
        oppoStatus = text("", 15, R.color.ink, false);
        add(card, oppoStatus, 0);
        add(card, text(getString(R.string.app_details, getPackageName(), OPPO_CHANNEL),
                13, R.color.ink_secondary, false), 10);
        appKey = input(R.string.oppo_app_key, InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD);
        appSecret = input(R.string.oppo_app_secret, InputType.TYPE_TEXT_VARIATION_PASSWORD);
        add(card, appKey, 14);
        add(card, appSecret, 8);
        registerButton = button(R.string.oppo_register, this::register);
        add(card, registerButton, 12);
        registrationId = text("", 13, R.color.ink, false);
        registrationId.setTextIsSelectable(true);
        add(card, registrationId, 12);
        add(content, card, 0);
        TextView description = text(getString(R.string.oppo_summary), 13, R.color.ink_secondary, false);
        add(content, settingsRow(R.drawable.ic_bell, R.string.oppo_notification_settings, description,
                () -> openNotificationSettings(OPPO_CHANNEL, false)), 12);
    }

    private void updateOppo() {
        if (page != Page.OPPO) {
            return;
        }
        boolean supported = HeytapPushManager.isSupportPush(getApplicationContext());
        boolean channelEnabled = notifications.getNotificationChannel(OPPO_CHANNEL).getImportance()
                != NotificationManager.IMPORTANCE_NONE;
        String state = registering ? getString(R.string.oppo_registering)
                : registration.getString("status", getString(R.string.oppo_not_registered));
        oppoStatus.setText(getString(R.string.oppo_status,
                getString(supported ? R.string.push_supported : R.string.push_unsupported),
                getString(notifications.areNotificationsEnabled() ? R.string.allowed : R.string.blocked),
                getString(channelEnabled ? R.string.allowed : R.string.blocked), state));
        String token = registration.getString("registration_id", "");
        registrationId.setVisibility(token.isEmpty() ? View.GONE : View.VISIBLE);
        registrationId.setText(token.isEmpty() ? "" : getString(R.string.oppo_registration_id, token));
        boolean enabled = supported && !registering;
        registerButton.setEnabled(enabled);
        registerButton.setAlpha(enabled ? 1f : 0.5f);
    }

    private void register() {
        String key = appKey.getText().toString().trim();
        String secret = appSecret.getText().toString().trim();
        if (key.isEmpty()) {
            appKey.setError(getString(R.string.oppo_key_required));
            return;
        }
        if (secret.isEmpty()) {
            appSecret.setError(getString(R.string.oppo_secret_required));
            return;
        }

        appSecret.getText().clear();
        registering = true;
        registration.edit().remove("registration_id").apply();
        updateOppo();
        RegistrationCallback callback = new RegistrationCallback(registration);
        Context context = getApplicationContext();
        try {
            HeytapPushManager.init(context, false);
            HeytapPushManager.register(context, key, secret, callback);
        } catch (RuntimeException error) {
            callback.finish("无法启动 OPPO 注册（" + error.getClass().getSimpleName() + "）", null);
        }
    }

    private View settingsRow(int glyph, int title, TextView description, Runnable action) {
        return settingsRow(glyph, getString(title), description, action);
    }

    private View settingsRow(int glyph, String title, TextView description, Runnable action) {
        return settingsRow(glyph, title, description, action, R.color.accent, R.color.surface);
    }

    private View settingsRow(int glyph, String title, TextView description, Runnable action, int tint, int surface) {
        LinearLayout item = row();
        item.setPadding(dp(13), dp(12), dp(13), dp(12));
        item.setBackground(shape(18, surface, R.color.line));
        item.addView(iconTile(glyph, tint, 39, tint == 0 ? 28 : 22, 12,
                tint == 0 ? surface : R.color.surface_tint));
        LinearLayout labels = column();
        add(labels, text(title, 15, R.color.ink, true), 0);
        add(labels, description, 3);
        LinearLayout.LayoutParams labelsLayout = new LinearLayout.LayoutParams(0, WRAP, 1f);
        labelsLayout.setMarginStart(dp(11));
        labelsLayout.setMarginEnd(dp(8));
        item.addView(labels, labelsLayout);
        item.addView(icon(R.drawable.ic_chevron, R.color.ink_secondary),
                new LinearLayout.LayoutParams(dp(20), dp(20)));
        clickable(item, 18, action);
        return item;
    }

    private EditText input(int hint, int variation) {
        EditText input = new EditText(this);
        input.setHint(hint);
        input.setTextSize(15);
        input.setTextColor(getColor(R.color.ink));
        input.setHintTextColor(getColor(R.color.ink_secondary));
        input.setSingleLine();
        input.setInputType(InputType.TYPE_CLASS_TEXT | variation);
        input.setSaveEnabled(false);
        input.setMinimumHeight(dp(48));
        input.setPadding(dp(12), dp(8), dp(12), dp(8));
        input.setBackground(shape(12, R.color.canvas, R.color.line));
        return input;
    }

    private Button button(int label, Runnable action) {
        Button button = new Button(this, null, 0);
        button.setAllCaps(false);
        button.setTextSize(15);
        button.setTypeface(MEDIUM);
        button.setGravity(Gravity.CENTER);
        button.setMinimumHeight(dp(48));
        button.setPadding(dp(14), dp(8), dp(14), dp(8));
        button.setOnClickListener(view -> action.run());
        styleButton(button, label, true);
        return button;
    }

    private void styleButton(Button button, int label, boolean filled) {
        button.setText(label);
        button.setTextColor(getColor(filled ? R.color.on_accent : R.color.accent));
        GradientDrawable background = shape(13, filled ? R.color.accent : R.color.surface,
                filled ? 0 : R.color.line);
        int ripple = filled ? R.color.on_accent : R.color.accent;
        button.setBackground(new RippleDrawable(
                ColorStateList.valueOf((getColor(ripple) & 0xFFFFFF) | 0x26000000), background, null));
    }

    private LinearLayout card() {
        LinearLayout card = column();
        card.setPadding(dp(16), dp(15), dp(16), dp(15));
        card.setBackground(shape(18, R.color.surface, R.color.line));
        return card;
    }

    private TextView heading(int label, int size) {
        TextView text = text(getString(label), size, R.color.ink, true);
        if (Build.VERSION.SDK_INT >= 28) {
            text.setAccessibilityHeading(true);
        }
        return text;
    }

    private TextView text(String value, int size, int color, boolean medium) {
        TextView text = new TextView(this);
        text.setText(value);
        text.setTextSize(size);
        text.setTextColor(getColor(color));
        if (medium) {
            text.setTypeface(MEDIUM);
        }
        return text;
    }

    private ImageView icon(int id, int tint) {
        ImageView view = new ImageView(this);
        view.setImageResource(id);
        if (tint != 0) {
            view.setImageTintList(ColorStateList.valueOf(getColor(tint)));
        }
        view.setImportantForAccessibility(View.IMPORTANT_FOR_ACCESSIBILITY_NO);
        return view;
    }

    private FrameLayout iconTile(int id, int tint, int size, int glyphSize, int radius, int fill) {
        FrameLayout tile = new FrameLayout(this);
        tile.setLayoutParams(new LinearLayout.LayoutParams(dp(size), dp(size)));
        if (fill != 0) {
            tile.setBackground(shape(radius, fill, 0));
        }
        tile.addView(icon(id, tint), new FrameLayout.LayoutParams(dp(glyphSize), dp(glyphSize), Gravity.CENTER));
        return tile;
    }

    private GradientDrawable shape(int radius, int fill, int stroke) {
        GradientDrawable shape = new GradientDrawable();
        shape.setCornerRadius(dp(radius));
        shape.setColor(fill == 0 ? 0 : getColor(fill));
        if (stroke != 0) {
            shape.setStroke(dp(1), getColor(stroke));
        }
        return shape;
    }

    private void clickable(View view, int radius, Runnable action) {
        GradientDrawable mask = shape(radius, 0, 0);
        mask.setColor(0xFFFFFFFF);
        view.setForeground(new RippleDrawable(ColorStateList.valueOf(getColor(R.color.ripple)), null, mask));
        view.setFocusable(true);
        view.setOnClickListener(clicked -> action.run());
    }

    private View divider() {
        View view = new View(this);
        view.setLayoutParams(new LinearLayout.LayoutParams(MATCH, dp(1)));
        view.setBackgroundColor(getColor(R.color.line));
        return view;
    }

    private void add(LinearLayout parent, View child, int marginTop) {
        ViewGroup.LayoutParams existing = child.getLayoutParams();
        LinearLayout.LayoutParams params =
                new LinearLayout.LayoutParams(MATCH, existing == null ? WRAP : existing.height);
        params.topMargin = dp(marginTop);
        parent.addView(child, params);
    }

    private LinearLayout row() {
        LinearLayout row = new LinearLayout(this);
        row.setOrientation(LinearLayout.HORIZONTAL);
        row.setGravity(Gravity.CENTER_VERTICAL);
        return row;
    }

    private LinearLayout column() {
        LinearLayout column = new LinearLayout(this);
        column.setOrientation(LinearLayout.VERTICAL);
        return column;
    }

    private int dp(int size) {
        return Math.round(size * getResources().getDisplayMetrics().density);
    }

    private static final class RegistrationCallback implements ICallBackResultService {
        private final SharedPreferences registration;
        private final Runnable timeout = () -> finish("注册超时，请检查网络和平台配置后重试", null);
        private boolean completed;

        RegistrationCallback(SharedPreferences registration) {
            this.registration = registration;
            MAIN.postDelayed(timeout, 45_000);
        }

        void finish(String status, String token) {
            MAIN.post(() -> {
                if (completed) {
                    return;
                }
                completed = true;
                registering = false;
                MAIN.removeCallbacks(timeout);
                registration.edit().putString("status", status)
                        .putString("registration_id", token).apply();
            });
        }

        @Override
        public void onRegister(int code, String token, String appPackage, String ignored) {
            if (code == 0 && !TextUtils.isEmpty(token)) {
                finish("OPPO 注册成功", token);
            } else {
                finish("OPPO 注册未完成，返回码 " + code, null);
            }
        }

        @Override
        public void onError(int code, String message, String appPackage, String ignored) {
            finish("OPPO SDK 返回错误码 " + code, null);
        }

        @Override public void onUnRegister(int code, String appPackage, String ignored) {}
        @Override public void onSetPushTime(int code, String ignored) {}
        @Override public void onGetPushStatus(int code, int ignored) {}
        @Override public void onGetNotificationStatus(int code, int ignored) {}
    }
}
