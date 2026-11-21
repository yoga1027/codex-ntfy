package local.codexntfy.opush;

import android.app.AlertDialog;
import android.content.Context;
import android.content.SharedPreferences;
import android.graphics.Typeface;
import android.graphics.drawable.GradientDrawable;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.text.InputFilter;
import android.text.InputType;
import android.text.Spannable;
import android.text.SpannableStringBuilder;
import android.text.TextUtils;
import android.text.style.BackgroundColorSpan;
import android.view.Gravity;
import android.view.View;
import android.view.inputmethod.InputMethodManager;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.ScrollView;
import android.widget.TextView;

import org.json.JSONArray;
import org.json.JSONException;
import org.json.JSONObject;

import java.io.IOException;
import java.math.BigDecimal;
import java.time.Instant;
import java.time.ZoneId;
import java.time.format.DateTimeFormatter;
import java.util.ArrayList;
import java.util.List;
import java.util.Locale;
import java.util.UUID;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.function.Consumer;

final class ConversationPane extends LinearLayout {
    private static final int TURN_WINDOW = 20;
    private static final int MATCH = LayoutParams.MATCH_PARENT;
    private static final int WRAP = LayoutParams.WRAP_CONTENT;
    private static final ZoneId BEIJING = ZoneId.of("Asia/Shanghai");
    private static final DateTimeFormatter TIMESTAMP =
            DateTimeFormatter.ofPattern("uuuu-MM-dd HH:mm:ss", Locale.ROOT).withZone(BEIJING);
    private static final DateTimeFormatter RESET_TIME =
            DateTimeFormatter.ofPattern("MM-dd HH:mm", Locale.ROOT).withZone(BEIJING);
    private final Handler main = new Handler(Looper.getMainLooper());
    private final ExecutorService worker = Executors.newSingleThreadExecutor();
    private final String threadId;
    private final boolean claude;
    private final String target;
    private final String attemptKey;
    private final Consumer<String> fork;
    private final ThreadStore store;
    private final ConversationClient client;
    private final SharedPreferences attempts;
    private final SharedPreferences modelPreferences;
    private final TranscriptView transcript;
    private final JSONArray reminderHeader = new JSONArray();
    private final LinearLayout footer;
    private final TextView feedback;
    private final EditText draft;
    private final Button send;
    private final TextView attachmentPicker;
    private final LinearLayout attachmentList;
    private final ReplyAttachments attachmentFiles;
    private final SharedPreferences attachmentDrafts;
    private JSONArray attachments;
    private boolean importingAttachment;
    private final TextView modelPicker;
    private final TextView effortPicker;
    private final TextView stopPicker;
    private final Runnable load = this::refresh;
    private JSONArray currentTurns;
    private int visibleTurns = TURN_WINDOW;
    private String revision = "";
    private String rendered = "";
    private String expectedTurn;
    private long submittedAt;
    private boolean visible;
    private boolean closed;
    private boolean loading;
    private boolean sending;
    private boolean running;
    private boolean loaded;
    private boolean online;
    private boolean archived;
    private boolean loadingModels;
    private String selectedModel;
    private String selectedModelLabel;
    private String selectedEffort;
    private String currentTurn;
    private String stopRequestedFor;
    private boolean stopping;
    private boolean changingFile;
    private JSONObject modelOptions;

    ConversationPane(MainActivity context, String threadId, ReminderHistory.Entry reminder, Consumer<String> fork) {
        super(context);
        this.threadId = threadId;
        claude = ThreadStore.isClaude(threadId);
        target = threadId == null ? reminder.id : threadId;
        attemptKey = threadId == null ? target : "thread:" + target;
        this.fork = fork;
        store = new ThreadStore(context);
        client = new ConversationClient(context);
        attempts = context.getSharedPreferences("phone_reply_attempts", Context.MODE_PRIVATE);
        attachmentDrafts = context.getSharedPreferences("reply_attachment_drafts", Context.MODE_PRIVATE);
        attachmentFiles = new ReplyAttachments(context);
        try {
            attachments = new JSONArray(attachmentDrafts.getString(attemptKey, "[]"));
        } catch (JSONException ignored) {
            attachments = new JSONArray();
        }
        modelPreferences = context.getSharedPreferences("conversation_model_choices", Context.MODE_PRIVATE);
        selectedModel = modelPreferences.getString("id:" + target, "");
        selectedModelLabel = modelPreferences.getString("label:" + target, "");
        selectedEffort = modelPreferences.getString("effort:" + target, "");
        setOrientation(VERTICAL);
        setFocusableInTouchMode(true);

        if (threadId == null) reminderHeader.put(reminder.title).put(reminder.body);
        transcript = new TranscriptView(context, this::transcriptAction);
        addView(transcript, new LayoutParams(MATCH, 0, 1f));
        empty(R.string.conversation_loading);

        footer = column();
        footer.setBackgroundColor(color(R.color.surface));
        footer.setPadding(dp(12), dp(7), dp(12), dp(9));
        feedback = text("", 12, R.color.ink_secondary, false);
        feedback.setPadding(dp(5), 0, dp(5), 0);
        feedback.setVisibility(GONE);
        add(footer, feedback, 0);
        LinearLayout controls = new LinearLayout(context);
        attachmentPicker = composerControl(() -> chooseAttachment(context));
        attachmentPicker.setText("+");
        attachmentPicker.setTextSize(23);
        attachmentPicker.setGravity(Gravity.CENTER);
        attachmentPicker.setContentDescription(string(R.string.conversation_attach));
        controls.addView(attachmentPicker, new LayoutParams(dp(42), WRAP));
        modelPicker = composerControl(() -> loadModelOptions(false));
        LayoutParams modelLayout = new LayoutParams(0, WRAP, 1f);
        modelLayout.setMarginStart(dp(5));
        controls.addView(modelPicker, modelLayout);
        effortPicker = composerControl(() -> loadModelOptions(true));
        addComposerControl(controls, effortPicker);
        stopPicker = composerControl(this::confirmInterrupt);
        stopPicker.setText(R.string.conversation_stop);
        addComposerControl(controls, stopPicker);
        add(footer, controls, 0);
        attachmentList = column();
        add(footer, attachmentList, 0);
        renderAttachments();
        updateModelPicker();
        LinearLayout composer = new LinearLayout(context);
        composer.setGravity(Gravity.BOTTOM);
        draft = new EditText(context);
        draft.setTextColor(color(R.color.ink));
        draft.setHintTextColor(color(R.color.ink_secondary));
        draft.setHint(replyHint());
        draft.setInputType(InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_FLAG_MULTI_LINE
                | InputType.TYPE_TEXT_FLAG_CAP_SENTENCES);
        draft.setFilters(new InputFilter[]{new InputFilter.LengthFilter(12000)});
        draft.setMaxLines(5);
        draft.setMinHeight(dp(48));
        draft.setTextSize(15);
        draft.setPadding(dp(13), dp(9), dp(13), dp(9));
        draft.setBackground(shape(R.color.canvas, 14));
        JSONObject previous = attempt();
        if (previous != null) {
            draft.setText(previous.optString("text"));
        }
        composer.addView(draft, new LayoutParams(0, WRAP, 1f));
        send = new Button(context);
        send.setText(R.string.conversation_send);
        send.setAllCaps(false);
        send.setMinimumWidth(0);
        send.setMinWidth(0);
        send.setPadding(0, 0, 0, 0);
        send.setMinHeight(dp(48));
        send.setTextColor(color(R.color.on_accent));
        send.setBackground(shape(R.color.accent, 14));
        send.setOnClickListener(view -> send());
        LayoutParams sendLayout = new LayoutParams(dp(52), WRAP);
        sendLayout.setMarginStart(dp(8));
        composer.addView(send, sendLayout);
        add(footer, composer, 3);
        addView(footer, new LayoutParams(MATCH, WRAP));
        footer.setVisibility(GONE);
    }

    void resume() {
        visible = true;
        schedule(0);
    }

    void pause() {
        visible = false;
        main.removeCallbacks(load);
    }

    void setArchived(boolean archived) {
        if (this.archived != archived) {
            this.archived = archived;
            draft.setHint(archived ? R.string.conversation_archived : replyHint());
            if (!archived && feedback.getText().equals(string(R.string.conversation_archived))) {
                feedback(0);
            }
            updateSend();
        }
    }

    void close() {
        closed = true;
        pause();
        worker.shutdownNow();
        removeView(transcript);
        transcript.destroy();
    }

    private void schedule(long delay) {
        main.removeCallbacks(load);
        if (visible && !closed) {
            main.postDelayed(load, delay);
        }
    }

    private void refresh() {
        if (!visible || closed || loading || sending) {
            return;
        }
        loading = true;
        String lastRevision = revision;
        boolean showSaved = !loaded && threadId != null;
        worker.execute(() -> {
            try {
                JSONObject saved = showSaved ? store.read(threadId) : null;
                if (saved != null) {
                    main.post(() -> {
                        if (!closed) {
                            update(saved);
                            updateSend();
                        }
                    });
                }
                String current = saved == null ? lastRevision : saved.optString("revision");
                JSONObject response = threadId == null ? client.read(target, current)
                        : client.readThread(threadId, current);
                JSONObject result = threadId != null && response != null && !response.optBoolean("unchanged")
                        ? store.save(threadId, response) : response;
                main.post(() -> {
                    if (closed) {
                        return;
                    }
                    loading = false;
                    online = result != null;
                    if (online && feedback.getText().equals(string(R.string.conversation_cached))) {
                        feedback(0);
                    }
                    if (result == null && !loaded) {
                        empty(R.string.conversation_unavailable);
                    } else if (result != null && !result.optBoolean("unchanged")) {
                        update(result);
                    }
                    updateSend();
                    schedule(result == null ? 12000 : (running || expectedTurn != null ? 2200 : 4800));
                });
            } catch (IOException | JSONException | SecurityException error) {
                main.post(() -> {
                    if (closed) {
                        return;
                    }
                    loading = false;
                    online = false;
                    if (!loaded) {
                        empty(R.string.conversation_offline);
                    } else if (feedback.getText().length() == 0) {
                        feedback(R.string.conversation_cached);
                    }
                    updateSend();
                    schedule(6000);
                });
            }
        });
    }

    private void empty(int message) {
        JSONObject data = transcriptData();
        put(data, "notice", string(message));
        transcript.show(data);
    }

    private JSONObject transcriptData() {
        JSONObject data = new JSONObject();
        put(data, "claude", claude);
        put(data, "header", reminderHeader);
        return data;
    }

    private static void put(JSONObject data, String key, Object value) {
        try {
            data.put(key, value);
        } catch (JSONException error) {
            throw new IllegalArgumentException(key, error);
        }
    }

    private void update(JSONObject snapshot) {
        JSONArray turns = snapshot.optJSONArray("turns");
        if (turns == null) {
            empty(R.string.conversation_unavailable);
            return;
        }
        revision = snapshot.optString("revision");
        JSONObject last = turns.optJSONObject(turns.length() - 1);
        running = last != null && "running".equals(last.optString("status"));
        currentTurn = running ? last.optString("id", null) : null;
        if (expectedTurn != null) {
            for (int i = 0; i < turns.length(); i++) {
                JSONObject turn = turns.optJSONObject(i);
                if (turn != null && expectedTurn.equals(turn.optString("id"))) {
                    expectedTurn = null;
                    if ("running".equals(turn.optString("status"))) {
                        feedback(runningLabel());
                    } else {
                        feedback(0);
                    }
                    break;
                }
            }
        } else if (feedback.getText().equals(string(runningLabel())) && !running) {
            feedback(0);
        }
        if (last != null && stopRequestedFor != null && !running
                && stopRequestedFor.equals(last.optString("id"))) {
            stopRequestedFor = null;
            feedback("interrupted".equals(last.optString("status")) ? R.string.conversation_stopped : 0);
        }

        String encoded = turns.toString();
        if (encoded.equals(rendered)) {
            return;
        }
        loaded = true;
        rendered = encoded;
        footer.setVisibility(VISIBLE);
        currentTurns = turns;
        renderTurns(false);
    }

    private void renderTurns(boolean prepend) {
        if (currentTurns.length() == 0) {
            empty(R.string.conversation_begin);
            return;
        }
        JSONObject data = transcriptData();
        int start = Math.max(0, currentTurns.length() - visibleTurns);
        if (start > 0) put(data, "earlier", string(R.string.conversation_earlier));
        put(data, "prepend", prepend);
        JSONArray turns = new JSONArray();
        for (int i = start; i < currentTurns.length(); i++) {
            JSONObject turn = currentTurns.optJSONObject(i);
            if (turn != null) turns.put(formatTurn(turn, i == start));
        }
        put(data, "turns", turns);
        transcript.show(data);
    }

    private JSONObject formatTurn(JSONObject turn, boolean first) {
        JSONObject formatted = new JSONObject();
        String id = turn.optString("id");
        put(formatted, "id", id);
        String status = turn.optString("status");
        boolean inProgress = "running".equals(status);
        if (!first && !"completed".equals(status) && !"quota".equals(status) && !"network".equals(status)) {
            put(formatted, "state", string(inProgress ? runningLabel() : R.string.conversation_ended));
        }
        JSONArray items = turn.optJSONArray("messages");
        JSONArray messages = new JSONArray();
        put(formatted, "messages", messages);
        String model = claude || inProgress || turn.isNull("model") ? "" : turn.optString("model");
        String timing = turnTiming(turn.optLong("elapsed_ms", -1), turn.optLong("completed_at_ms", -1), model);
        for (int i = 0; items != null && i < items.length(); i++) {
            JSONObject item = items.optJSONObject(i);
            if (item == null) continue;
            String kind = item.optString("kind");
            boolean fromUser = "user".equals(kind);
            JSONObject message = new JSONObject();
            put(message, "text", item.optString("text"));
            put(message, "kind", kind);
            if (!fromUser && !timing.isEmpty()) {
                put(message, "timing", timing);
                timing = "";
            }
            int tag = fromUser ? R.string.conversation_you : ("progress".equals(kind)
                    ? R.string.conversation_progress : (claude ? R.string.conversation_claude : R.string.conversation_assistant));
            long sentAt = item.optLong("sent_at_ms", -1);
            put(message, "label", fromUser && sentAt >= 0
                    ? string(R.string.conversation_timing, string(tag), TIMESTAMP.format(Instant.ofEpochMilli(sentAt)))
                    : string(tag));
            messages.put(message);
        }
        if (!timing.isEmpty()) put(formatted, "timing", timing);
        if (first && messages.length() == 0) put(formatted, "notice", string(R.string.conversation_empty_turn));
        if (!inProgress && !turn.isNull("files_edited")) {
            put(formatted, "editLabel", string(R.string.conversation_files, turn.optInt("files_edited")));
            put(formatted, "files_edited", turn.optInt("files_edited"));
            put(formatted, "files", turn.optJSONArray("files"));
            if (threadId != null && !id.isEmpty()) {
                put(formatted, "branch", string(R.string.fork_conversation));
                put(formatted, "editable", true);
            }
            if (!"quota".equals(status)) {
                put(formatted, "quota", quotaLine(turn));
            }
        }
        if ("quota".equals(status)) {
            put(formatted, "ending", string(R.string.conversation_quota_ending, quotaLine(turn)));
        } else if ("network".equals(status)) {
            put(formatted, "ending", string(R.string.conversation_network_ending));
        }
        return formatted;
    }

    private String turnTiming(long elapsedMs, long completedAt, String model) {
        String label = elapsedMs >= 0 ? formatDuration(elapsedMs) : "";
        if (completedAt >= 0) {
            String timestamp = TIMESTAMP.format(Instant.ofEpochMilli(completedAt));
            label = label.isEmpty() ? timestamp : string(R.string.conversation_timing, label, timestamp);
        }
        if (!model.isEmpty()) {
            label = label.isEmpty() ? model : string(R.string.conversation_timing, label, model);
        }
        return label;
    }

    private void transcriptAction(Uri uri) {
        if (closed) return;
        String path = uri.getQueryParameter("path");
        String turn = uri.getQueryParameter("turn");
        switch (uri.getLastPathSegment()) {
            case "earlier":
                visibleTurns += TURN_WINDOW;
                if (currentTurns != null) renderTurns(true);
                break;
            case "fork":
                if (threadId != null && turn != null) fork.accept(turn);
                break;
            case "file":
                if (threadId == null || path == null) break;
                String line = uri.getQueryParameter("line");
                try {
                    openFile(path, line == null ? 1 : Math.max(1, Integer.parseInt(line)));
                } catch (NumberFormatException ignored) {
                    message(string(R.string.conversation_file_failed));
                }
                break;
            case "undo":
            case "redo":
                if (threadId != null && turn != null && path != null) {
                    confirmFileChange(turn, path, "undo".equals(uri.getLastPathSegment()));
                }
                break;
        }
    }

    private void openFile(String path, int line) {
        ScrollView scroll = new ScrollView(getContext());
        TextView contents = text(string(R.string.conversation_loading), 13, R.color.ink, false);
        contents.setPadding(dp(16), dp(8), dp(16), dp(12));
        contents.setTypeface(Typeface.MONOSPACE);
        contents.setTextIsSelectable(true);
        scroll.addView(contents);
        AlertDialog dialog = new AlertDialog.Builder(getContext()).setTitle(path.substring(path.lastIndexOf('/') + 1))
                .setView(scroll).setNeutralButton(R.string.conversation_file_previous, null)
                .setPositiveButton(R.string.conversation_file_next, null)
                .setNegativeButton(android.R.string.cancel, null).show();
        loadFile(dialog, scroll, contents, path, line);
    }

    private void loadFile(AlertDialog dialog, ScrollView scroll, TextView contents, String path, int line) {
        Button previous = dialog.getButton(AlertDialog.BUTTON_NEUTRAL);
        Button next = dialog.getButton(AlertDialog.BUTTON_POSITIVE);
        previous.setEnabled(false);
        next.setEnabled(false);
        contents.setText(R.string.conversation_loading);
        worker.execute(() -> {
            JSONObject result;
            try {
                result = client.filePreview(threadId, path, line);
            } catch (IOException | JSONException | SecurityException ignored) {
                result = null;
            }
            JSONObject snapshot = result;
            main.post(() -> {
                if (closed || !dialog.isShowing()) return;
                if (snapshot == null || !"ok".equals(snapshot.optString("status"))) {
                    contents.setText(fileMessage(snapshot));
                    return;
                }
                int start = snapshot.optInt("start");
                int at = snapshot.optInt("line");
                int total = snapshot.optInt("total");
                JSONArray lines = snapshot.optJSONArray("lines");
                SpannableStringBuilder shown = new SpannableStringBuilder();
                int focus = 0;
                for (int i = 0; lines != null && i < lines.length(); i++) {
                    int number = start + i;
                    int offset = shown.length();
                    shown.append(String.format(Locale.ROOT, "%4d  %s", number, lines.optString(i)));
                    if (number == at) {
                        focus = offset;
                        shown.setSpan(new BackgroundColorSpan(color(R.color.surface_tint)), focus, shown.length(),
                                Spannable.SPAN_EXCLUSIVE_EXCLUSIVE);
                    }
                    shown.append('\n');
                }
                contents.setText(shown);
                int focusOffset = focus;
                contents.post(() -> {
                    if (contents.getLayout() == null) return;
                    scroll.scrollTo(0, Math.max(0, contents.getLayout().getLineTop(
                            contents.getLayout().getLineForOffset(focusOffset)) - dp(22)));
                });
                int end = start + (lines == null ? 0 : lines.length());
                previous.setEnabled(start > 1);
                next.setEnabled(end <= total);
                previous.setOnClickListener(view -> loadFile(dialog, scroll, contents, path, Math.max(1, start - 105)));
                next.setOnClickListener(view -> loadFile(dialog, scroll, contents, path, end + 15));
            });
        });
    }

    private void confirmFileChange(String turn, String path, boolean undo) {
        if (changingFile) return;
        int title = undo ? R.string.conversation_undo : R.string.conversation_redo;
        new AlertDialog.Builder(getContext()).setTitle(title)
                .setMessage(string(R.string.conversation_file_confirmation, path))
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(android.R.string.ok, (dialog, button) -> changeFile(turn, path, undo)).show();
    }

    private void changeFile(String turn, String path, boolean undo) {
        changingFile = true;
        feedback(R.string.conversation_file_changing);
        worker.execute(() -> {
            JSONObject result;
            try {
                result = client.changeFile(threadId, turn, path, undo ? "undo" : "redo");
            } catch (IOException | JSONException | SecurityException ignored) {
                result = null;
            }
            JSONObject response = result;
            main.post(() -> {
                if (closed) return;
                changingFile = false;
                feedback(0);
                String status = response == null ? "" : response.optString("status");
                if ("undone".equals(status) || "applied".equals(status)) {
                    feedback(undo ? R.string.conversation_undone : R.string.conversation_redone);
                } else {
                    message(fileMessage(response));
                }
            });
        });
    }

    private String fileMessage(JSONObject result) {
        if (result != null) {
            if ("busy".equals(result.optString("status"))) return string(R.string.conversation_busy);
            if ("missing".equals(result.optString("status"))) return string(R.string.conversation_file_missing);
            String detail = result.optString("message");
            if (!detail.isEmpty()) return detail;
        }
        return string(R.string.conversation_file_failed);
    }

    private void message(String detail) {
        new AlertDialog.Builder(getContext()).setMessage(detail).setPositiveButton(android.R.string.ok, null).show();
    }

    private String formatDuration(long elapsedMs) {
        long seconds = elapsedMs / 1000;
        if (seconds >= 3600) {
            return string(R.string.conversation_duration_hours, seconds / 3600,
                    seconds / 60 % 60, seconds % 60);
        }
        if (seconds >= 60) {
            return string(R.string.conversation_duration_minutes, seconds / 60, seconds % 60);
        }
        return string(R.string.conversation_duration_seconds, seconds);
    }

    private String quotaLine(JSONObject turn) {
        JSONObject remaining = turn.optJSONObject("quota_remaining_percent");
        JSONObject resets = turn.optJSONObject("quota_reset_ms");
        return string(R.string.conversation_quota_reset,
                quotaPercent(remaining, "300"), quotaPercent(remaining, "10080"),
                quotaReset(resets, "300"), quotaReset(resets, "10080"));
    }

    private String quotaPercent(JSONObject remaining, String window) {
        double value = remaining == null ? Double.NaN : remaining.optDouble(window);
        if (!(value >= 0 && value <= 100)) return string(R.string.conversation_quota_unknown);
        return BigDecimal.valueOf(value).stripTrailingZeros().toPlainString() + "%";
    }

    private String quotaReset(JSONObject resets, String window) {
        long resetAt = resets == null ? -1 : resets.optLong(window, -1);
        return resetAt >= 0 ? RESET_TIME.format(Instant.ofEpochMilli(resetAt))
                : string(R.string.conversation_quota_unknown);
    }

    private void chooseAttachment(MainActivity activity) {
        if (attachments.length() >= ReplyAttachments.MAX_FILES) {
            feedback(R.string.conversation_attachment_limit);
            return;
        }
        String[] options = {string(R.string.conversation_take_photo), string(R.string.conversation_upload_image),
                string(R.string.conversation_upload_file)};
        new AlertDialog.Builder(activity).setTitle(R.string.conversation_attach)
                .setItems(options, (dialog, action) -> activity.pickReplyAttachment(target, action))
                .setNegativeButton(android.R.string.cancel, null).show();
    }

    boolean matchesAttachmentTarget(String id) {
        return !closed && target.equals(id);
    }

    void importAttachment(Uri uri, boolean image, String cameraFile) {
        long used = 0;
        for (int i = 0; i < attachments.length(); i++) used += attachments.optJSONObject(i).optLong("size");
        long remaining = ReplyAttachments.MAX_BYTES - used;
        if (attachments.length() >= ReplyAttachments.MAX_FILES || remaining <= 0) {
            if (cameraFile != null) CameraOutput.file(getContext(), cameraFile).delete();
            feedback(R.string.conversation_attachment_limit);
            return;
        }
        importingAttachment = true;
        updateSend();
        feedback(R.string.conversation_attachment_importing);
        worker.execute(() -> {
            JSONObject imported;
            try {
                imported = attachmentFiles.importAttachment(uri, image, cameraFile != null, remaining);
            } catch (IOException | JSONException | RuntimeException error) {
                imported = null;
            } finally {
                if (cameraFile != null) CameraOutput.file(getContext(), cameraFile).delete();
            }
            JSONObject result = imported;
            main.post(() -> {
                if (closed) {
                    if (result != null) attachmentFiles.remove(result);
                    return;
                }
                importingAttachment = false;
                if (result == null) {
                    feedback(R.string.conversation_attachment_failed);
                } else {
                    attachments.put(result);
                    saveAttachments();
                    feedback(0);
                }
                updateSend();
            });
        });
    }

    private void saveAttachments() {
        attachmentDrafts.edit().putString(attemptKey, attachments.toString()).apply();
        renderAttachments();
    }

    private void renderAttachments() {
        attachmentList.removeAllViews();
        for (int i = 0; i < attachments.length(); i++) {
            int position = i;
            JSONObject item = attachments.optJSONObject(i);
            LinearLayout row = new LinearLayout(getContext());
            row.setGravity(Gravity.CENTER_VERTICAL);
            TextView name = text(item.optString("name"), 13, R.color.ink, false);
            name.setSingleLine();
            name.setEllipsize(TextUtils.TruncateAt.MIDDLE);
            row.addView(name, new LayoutParams(0, WRAP, 1f));
            TextView remove = composerControl(() -> {
                if (sending || importingAttachment || archived) return;
                attachmentFiles.remove(item);
                attachments.remove(position);
                saveAttachments();
            });
            remove.setText("×");
            remove.setTextSize(20);
            remove.setContentDescription(string(R.string.conversation_remove_attachment, item.optString("name")));
            row.addView(remove, new LayoutParams(dp(42), WRAP));
            add(attachmentList, row, 1);
        }
    }

    private void send() {
        String text = draft.getText().toString().trim();
        if ((text.isEmpty() && attachments.length() == 0) || sending || running || archived || importingAttachment) {
            return;
        }
        JSONObject last = attempt();
        JSONArray selected = attachments;
        String selection = selected.toString();
        boolean retry = last != null && text.equals(last.optString("text")) &&
                selection.equals(last.optString("attachments", "[]"));
        String request = retry ? last.optString("id") : UUID.randomUUID().toString();
        String model = retry ? last.optString("model") : selectedModel;
        String effort = retry ? last.optString("effort") : selectedEffort;
        try {
            JSONObject entry = new JSONObject().put("id", request).put("text", text)
                    .put("model", model).put("effort", effort).put("attachments", selection);
            if (!attempts.edit().putString(attemptKey, entry.toString()).commit()) {
                feedback(R.string.conversation_send_failed);
                return;
            }
        } catch (JSONException error) {
            feedback(R.string.conversation_send_failed);
            return;
        }
        sending = true;
        main.removeCallbacks(load);
        feedback(R.string.conversation_sending);
        updateSend();
        worker.execute(() -> {
            try {
                JSONArray payload = attachmentFiles.encode(selected);
                JSONObject result = client.send(target, threadId != null, request, text, model, effort, payload);
                main.post(() -> sent(result, request, selected));
            } catch (IOException | JSONException | SecurityException error) {
                main.post(() -> {
                    if (!closed) {
                        sending = false;
                        feedback(R.string.conversation_verify_send);
                        updateSend();
                        schedule(0);
                    }
                });
            }
        });
    }

    private void sent(JSONObject response, String request, JSONArray sentFiles) {
        String status = response.optString("status");
        if (!status.isEmpty() && !"unknown".equals(status)) {
            JSONObject attempt = attempt();
            if (attempt != null && request.equals(attempt.optString("id"))) {
                attempts.edit().remove(attemptKey).apply();
            }
        }
        if ("accepted".equals(status) && sentFiles.length() > 0
                && sentFiles.toString().equals(attachmentDrafts.getString(attemptKey, ""))) {
            attachmentDrafts.edit().remove(attemptKey).apply();
            for (int index = 0; index < sentFiles.length(); index++) attachmentFiles.remove(sentFiles.optJSONObject(index));
            attachments = new JSONArray();
            if (!closed) renderAttachments();
        }
        if (closed) {
            return;
        }
        sending = false;
        if ("accepted".equals(status)) {
            expectedTurn = response.optString("turn_id");
            submittedAt = android.os.SystemClock.elapsedRealtime();
            draft.setText("");
            InputMethodManager keyboard = getContext().getSystemService(InputMethodManager.class);
            keyboard.hideSoftInputFromWindow(draft.getWindowToken(), 0);
            feedback(R.string.conversation_sent);
        } else if ("busy".equals(status)) {
            feedback(R.string.conversation_busy);
        } else if ("desktop_unavailable".equals(status)) {
            feedback(claude ? R.string.conversation_claude_open : R.string.conversation_open_desktop);
        } else if ("unavailable".equals(status)) {
            feedback(R.string.conversation_unavailable);
        } else if ("model_unavailable".equals(status)) {
            feedback(R.string.conversation_model_unavailable);
        } else if ("effort_unavailable".equals(status)) {
            feedback(R.string.conversation_effort_unavailable);
        } else if ("archived".equals(status)) {
            setArchived(true);
            feedback(R.string.conversation_archived);
        } else {
            feedback(R.string.conversation_verify_send);
        }
        updateSend();
        schedule(0);
    }

    private JSONObject attempt() {
        String saved = attempts.getString(attemptKey, null);
        if (saved == null) {
            return null;
        }
        try {
            JSONObject pending = new JSONObject(saved);
            return pending.optString("id").isEmpty() ? null : pending;
        } catch (JSONException error) {
            return null;
        }
    }

    private void updateSend() {
        if (expectedTurn != null && android.os.SystemClock.elapsedRealtime() - submittedAt > 60000) {
            expectedTurn = null;
            feedback(R.string.conversation_verify_send);
        }
        boolean available = loaded && online && !sending && !running && !archived && !importingAttachment && expectedTurn == null;
        attachmentPicker.setEnabled(!sending && !archived && !importingAttachment);
        send.setEnabled(available);
        send.setAlpha(available ? 1f : 0.55f);
        draft.setEnabled(!sending && !archived);
        updateModelPicker();
    }

    private void updateModelPicker() {
        modelPicker.setText(loadingModels ? string(R.string.conversation_model_loading)
                : string(R.string.conversation_model_display, selectedModelLabel.isEmpty()
                        ? string(R.string.conversation_model_default) : selectedModelLabel));
        effortPicker.setText(string(R.string.conversation_effort_display, effortLabel(selectedEffort)));
        boolean canChoose = !sending && !archived && !loadingModels;
        modelPicker.setVisibility(threadId == null ? GONE : VISIBLE);
        effortPicker.setVisibility(threadId == null ? GONE : VISIBLE);
        stopPicker.setVisibility(threadId == null ? GONE : VISIBLE);
        modelPicker.setEnabled(canChoose);
        effortPicker.setEnabled(canChoose);
        stopPicker.setEnabled(online && !sending && !stopping && activeTurn() != null);
        stopPicker.setAlpha(stopPicker.isEnabled() ? 1f : 0.48f);
    }

    private TextView composerControl(Runnable action) {
        TextView control = text("", 13, R.color.accent, true);
        control.setGravity(Gravity.CENTER_VERTICAL);
        control.setPadding(dp(9), 0, dp(9), 0);
        control.setMinHeight(dp(42));
        control.setMaxLines(1);
        control.setEllipsize(TextUtils.TruncateAt.END);
        control.setBackground(shape(R.color.canvas, 12));
        control.setOnClickListener(view -> action.run());
        return control;
    }

    private void addComposerControl(LinearLayout row, View control) {
        LinearLayout.LayoutParams placement = new LinearLayout.LayoutParams(WRAP, WRAP);
        placement.setMarginStart(dp(5));
        row.addView(control, placement);
    }

    private String activeTurn() {
        return currentTurn != null ? currentTurn : expectedTurn;
    }

    private void confirmInterrupt() {
        String turn = activeTurn();
        if (threadId == null || turn == null || stopping) {
            return;
        }
        new AlertDialog.Builder(getContext()).setTitle(R.string.conversation_stop_title)
                .setMessage(R.string.conversation_stop_confirmation)
                .setNegativeButton(android.R.string.cancel, null)
                .setPositiveButton(R.string.conversation_stop_confirm, (dialog, button) -> interrupt(turn)).show();
    }

    private void interrupt(String turn) {
        if (closed || stopping || !turn.equals(activeTurn())) {
            return;
        }
        stopping = true;
        updateModelPicker();
        worker.execute(() -> {
            String status = "";
            try {
                status = client.interrupt(threadId, turn).optString("status");
            } catch (IOException | JSONException | SecurityException ignored) {
                // Refresh the turn before offering another stop.
            }
            String result = status;
            main.post(() -> {
                if (closed) {
                    return;
                }
                stopping = false;
                stopRequestedFor = "requested".equals(result) && turn.equals(activeTurn()) ? turn : null;
                if ("requested".equals(result) && stopRequestedFor == null) {
                    updateSend();
                    return;
                }
                feedback("requested".equals(result) ? R.string.conversation_stop_requested
                        : ("not_running".equals(result) ? R.string.conversation_stop_already_ended
                            : (claude && "unavailable".equals(result) ? R.string.conversation_stop_claude_desktop
                                : R.string.conversation_stop_failed)));
                updateSend();
                schedule(0);
            });
        });
    }

    private void loadModelOptions(boolean effort) {
        if (threadId == null || loadingModels || closed || archived) {
            return;
        }
        loadingModels = true;
        updateModelPicker();
        worker.execute(() -> {
            JSONObject options = null;
            try {
                options = client.models(threadId);
            } catch (IOException | JSONException | SecurityException ignored) {
                // Default remains available without a completed model lookup.
            }
            JSONObject result = options;
            main.post(() -> {
                if (closed) {
                    return;
                }
                loadingModels = false;
                modelOptions = result;
                updateModelPicker();
                if (visible) {
                    if (effort) {
                        showEffortChoices();
                    } else {
                        showModelChoices(result == null ? null : result.optJSONArray("models"));
                    }
                }
            });
        });
    }

    private void showModelChoices(JSONArray result) {
        List<String> ids = new ArrayList<>();
        List<String> labels = new ArrayList<>();
        ids.add("");
        labels.add(string(R.string.conversation_model_default));
        for (int i = 0; result != null && i < result.length(); i++) {
            JSONObject option = result.optJSONObject(i);
            if (option != null && !option.optString("id").isEmpty()) {
                ids.add(option.optString("id"));
                labels.add(option.optString("label", option.optString("id")));
            }
        }
        if (result == null) {
            feedback(R.string.conversation_model_load_failed);
        }
        new AlertDialog.Builder(getContext()).setTitle(R.string.conversation_model_choose)
                .setSingleChoiceItems(labels.toArray(new String[0]), ids.indexOf(selectedModel), (dialog, index) -> {
                    dialog.dismiss();
                    String model = ids.get(index);
                    String effort = selectedEffort;
                    if (!model.equals(selectedModel) && !effort.isEmpty() && !efforts(model).contains(effort)) {
                        effort = "";
                    }
                    saveSelection(model, index == 0 ? "" : labels.get(index), effort);
                }).setNegativeButton(android.R.string.cancel, null).show();
    }

    private void showEffortChoices() {
        List<String> ids = new ArrayList<>();
        ids.add("");
        ids.addAll(efforts(selectedModel));
        String[] labels = new String[ids.size()];
        for (int i = 0; i < labels.length; i++) {
            labels[i] = effortLabel(ids.get(i));
        }
        if (modelOptions == null) {
            feedback(R.string.conversation_model_load_failed);
        }
        new AlertDialog.Builder(getContext()).setTitle(R.string.conversation_effort_choose)
                .setSingleChoiceItems(labels, ids.indexOf(selectedEffort), (dialog, index) -> {
                    dialog.dismiss();
                    saveSelection(selectedModel, selectedModelLabel, ids.get(index));
                }).setNegativeButton(android.R.string.cancel, null).show();
    }

    private List<String> efforts(String model) {
        List<String> levels = new ArrayList<>();
        if (modelOptions == null) {
            return levels;
        }
        JSONArray supported = model.isEmpty() ? modelOptions.optJSONArray("default_efforts") : null;
        JSONArray models = modelOptions.optJSONArray("models");
        for (int i = 0; !model.isEmpty() && models != null && i < models.length(); i++) {
            JSONObject choice = models.optJSONObject(i);
            if (choice != null && model.equals(choice.optString("id"))) {
                supported = choice.optJSONArray("efforts");
                break;
            }
        }
        for (int i = 0; supported != null && i < supported.length(); i++) {
            String level = supported.optString(i);
            if (!level.isEmpty() && !levels.contains(level)) {
                levels.add(level);
            }
        }
        return levels;
    }

    private void saveSelection(String model, String label, String effort) {
        JSONObject pending = attempt();
        if (pending != null && draft.getText().toString().trim().equals(pending.optString("text"))
                && (!model.equals(pending.optString("model")) || !effort.equals(pending.optString("effort")))) {
            feedback(R.string.conversation_verify_send);
            return;
        }
        selectedModel = model;
        selectedModelLabel = label;
        selectedEffort = effort;
        modelPreferences.edit().putString("id:" + target, model).putString("label:" + target, label)
                .putString("effort:" + target, effort).apply();
        updateModelPicker();
    }

    private String effortLabel(String effort) {
        switch (effort) {
            case "": return string(R.string.conversation_effort_default);
            case "none": return string(R.string.conversation_effort_none);
            case "minimal": return string(R.string.conversation_effort_minimal);
            case "low": return string(R.string.conversation_effort_low);
            case "medium": return string(R.string.conversation_effort_medium);
            case "high": return string(R.string.conversation_effort_high);
            case "xhigh": return string(R.string.conversation_effort_xhigh);
            case "max": return string(R.string.conversation_effort_max);
            case "ultra": return string(R.string.conversation_effort_ultra);
            default: return effort;
        }
    }

    private void feedback(int id) {
        feedback.setVisibility(id == 0 ? GONE : VISIBLE);
        feedback.setText(id == 0 ? "" : string(id));
    }

    private int runningLabel() {
        return claude ? R.string.conversation_claude_running : R.string.conversation_running;
    }

    private int replyHint() {
        return claude ? R.string.conversation_claude_reply : R.string.conversation_reply_hint;
    }

    private void add(LinearLayout parent, View view, int top) {
        LayoutParams params = new LayoutParams(MATCH, WRAP);
        params.topMargin = dp(top);
        parent.addView(view, params);
    }

    private LinearLayout column() {
        LinearLayout column = new LinearLayout(getContext());
        column.setOrientation(VERTICAL);
        return column;
    }

    private GradientDrawable shape(int fill, int radius) {
        GradientDrawable shape = new GradientDrawable();
        shape.setColor(color(fill));
        shape.setCornerRadius(dp(radius));
        return shape;
    }

    private TextView text(String value, int size, int color, boolean bold) {
        TextView view = new TextView(getContext());
        view.setText(value);
        view.setTextSize(size);
        view.setTextColor(color(color));
        if (bold) {
            view.setTypeface(Typeface.create("sans-serif-medium", Typeface.NORMAL));
        }
        return view;
    }

    private String string(int id, Object... args) {
        return getContext().getString(id, args);
    }

    private int color(int id) {
        return getContext().getColor(id);
    }

    private int dp(int value) {
        return Math.round(getResources().getDisplayMetrics().density * value);
    }
}
