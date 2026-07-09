import React, { FormEvent, KeyboardEvent, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { css } from '@emotion/css';
import { GrafanaTheme2, PageLayoutType, PluginMeta } from '@grafana/data';
import { Alert, Badge, Button, IconButton, TextArea, useStyles2 } from '@grafana/ui';
import { PluginPage } from '@grafana/runtime';
import MarkdownIt from 'markdown-it';
import {
  deriveWebSocketUrl,
  loadAssistantHealth,
  loadAssistantSettings,
  sendAssistantMessage,
  withThreadId,
} from '../services/assistant';
import { AppPluginSettings, AssistantMessage, AssistantPacket, AssistantToolCall } from '../types';
import { testIds } from '../components/testIds';

type AssistantPageProps = {
  meta: PluginMeta<AppPluginSettings>;
};

type ConnectionState = 'connecting' | 'connected' | 'disconnected' | 'error';

const threadStorageKey = 'eclss.assistant.threadId';

function createId() {
  if (window.crypto?.randomUUID) {
    return window.crypto.randomUUID();
  }
  return `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function loadThreadId() {
  const saved = window.localStorage.getItem(threadStorageKey);
  if (saved) {
    return saved;
  }
  const next = createId();
  window.localStorage.setItem(threadStorageKey, next);
  return next;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function formatValue(value: unknown): string {
  if (value === undefined || value === null || value === '') {
    return '';
  }
  if (typeof value === 'string') {
    return value;
  }
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function compactText(value: string, maxLength = 360): string {
  const normalized = value.replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxLength) {
    return normalized;
  }
  return `${normalized.slice(0, maxLength).trim()}...`;
}

function normalizeToolCall(toolCall: AssistantToolCall): AssistantToolCall {
  const raw = toolCall as Record<string, unknown>;
  const parameters = toolCall.parameters ?? raw.args ?? raw.arguments ?? raw.input;
  const result = toolCall.result ?? raw.output ?? raw.content;

  return {
    tool: String(toolCall.tool || raw.name || raw.tool_name || raw.toolName || 'tool'),
    parameters: isRecord(parameters) ? parameters : parameters === undefined ? undefined : { value: parameters },
    result: formatValue(result),
  };
}

function AssistantPage({ meta }: AssistantPageProps) {
  const s = useStyles2(getStyles);
  const configuredApiUrl = meta.jsonData?.assistantApiUrl || meta.jsonData?.apiUrl || '';
  const configuredWsUrl = meta.jsonData?.assistantWsUrl || '';
  const [threadId, setThreadId] = useState(loadThreadId);
  const [apiUrl, setApiUrl] = useState(configuredApiUrl);
  const [wsUrl, setWsUrl] = useState(configuredWsUrl);
  const [apiKeyConfigured, setApiKeyConfigured] = useState(false);
  const [connection, setConnection] = useState<ConnectionState>('connecting');
  const [healthError, setHealthError] = useState('');
  const [input, setInput] = useState('');
  const [messages, setMessages] = useState<AssistantMessage[]>([
    {
      id: createId(),
      role: 'assistant',
      content: 'Hello! How can I assist you today with your observability data or Grafana dashboards?',
      status: 'done',
      thinking: [],
      toolCalls: [],
    },
  ]);
  const [isStreaming, setIsStreaming] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);
  const activeAssistantIdRef = useRef<string | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  const markdown = useMemo(
    () => {
      const parser = new MarkdownIt({
        breaks: false,
        html: false,
        linkify: true,
      });
      const defaultLinkOpen =
        parser.renderer.rules.link_open ??
        ((tokens, index, options, env, self) => self.renderToken(tokens, index, options));

      parser.renderer.rules.link_open = (tokens, index, options, env, self) => {
        const token = tokens[index];
        token.attrSet('target', '_blank');
        token.attrSet('rel', 'noreferrer');
        return defaultLinkOpen(tokens, index, options, env, self);
      };

      return parser;
    },
    []
  );

  useEffect(() => {
    let cancelled = false;

    async function loadSettings() {
      try {
        const settings = await loadAssistantSettings();
        if (cancelled) {
          return;
        }
        setApiUrl(settings.assistantApiUrl || configuredApiUrl);
        setWsUrl(settings.assistantWsUrl || configuredWsUrl);
        setApiKeyConfigured(settings.apiKeyConfigured);
      } catch (error) {
        if (!cancelled) {
          setHealthError(error instanceof Error ? error.message : 'Unable to load assistant settings.');
        }
      }

      try {
        await loadAssistantHealth();
        if (!cancelled) {
          setHealthError('');
        }
      } catch (error) {
        if (!cancelled) {
          setHealthError(error instanceof Error ? error.message : 'Assistant API health check failed.');
        }
      }
    }

    loadSettings();

    return () => {
      cancelled = true;
    };
  }, [configuredApiUrl, configuredWsUrl]);

  const wsEndpoint = useMemo(() => {
    if (wsUrl) {
      return withThreadId(wsUrl, threadId);
    }
    return deriveWebSocketUrl(apiUrl, threadId);
  }, [apiUrl, threadId, wsUrl]);

  const appendToActiveAssistant = useCallback((content: string) => {
    const activeId = activeAssistantIdRef.current;
    if (!activeId) {
      return;
    }
    setMessages((current) =>
      current.map((message) =>
        message.id === activeId
          ? {
              ...message,
              content: `${message.content}${content}`,
              status: 'streaming',
            }
          : message
      )
    );
  }, []);

  const addToolCallToActiveAssistant = useCallback((toolCall: AssistantToolCall) => {
    const activeId = activeAssistantIdRef.current;
    if (!activeId) {
      return;
    }
    setMessages((current) =>
      current.map((message) =>
        message.id === activeId
          ? {
              ...message,
              toolCalls: [...(message.toolCalls || []), normalizeToolCall(toolCall)],
            }
          : message
      )
    );
  }, []);

  const addThinkingToActiveAssistant = useCallback((content: string) => {
    const activeId = activeAssistantIdRef.current;
    const normalized = content.trim();
    if (!activeId || !normalized) {
      return;
    }

    setMessages((current) =>
      current.map((message) => {
        if (message.id !== activeId) {
          return message;
        }

        const thinking = message.thinking || [];
        if (thinking.includes(normalized)) {
          return message;
        }

        return {
          ...message,
          thinking: [...thinking, normalized],
        };
      })
    );
  }, []);

  const finishActiveAssistant = useCallback((status: AssistantMessage['status'] = 'done') => {
    const activeId = activeAssistantIdRef.current;
    if (activeId) {
      setMessages((current) =>
        current.map((message) =>
          message.id === activeId
            ? {
                ...message,
                status,
                content: message.content || (status === 'error' ? 'The assistant request failed.' : message.content),
              }
            : message
        )
      );
    }
    activeAssistantIdRef.current = null;
    setIsStreaming(false);
  }, []);

  const handlePacket = useCallback(
    (packet: AssistantPacket) => {
      if (packet.type === 'token') {
        appendToActiveAssistant(packet.content);
        return;
      }

      if (packet.type === 'message' && packet.content) {
        appendToActiveAssistant(packet.content);
        return;
      }

      if (packet.type === 'tool_call') {
        addToolCallToActiveAssistant(packet);
        return;
      }

      if (packet.type === 'thinking') {
        addThinkingToActiveAssistant(packet.content);
        return;
      }

      if (packet.type === 'done') {
        finishActiveAssistant('done');
        return;
      }

      if (packet.type === 'error') {
        appendToActiveAssistant(packet.content);
        finishActiveAssistant('error');
      }
    },
    [addThinkingToActiveAssistant, addToolCallToActiveAssistant, appendToActiveAssistant, finishActiveAssistant]
  );

  useEffect(() => {
    if (!wsEndpoint || typeof WebSocket === 'undefined') {
      setConnection('disconnected');
      return;
    }

    setConnection('connecting');
    const socket = new WebSocket(wsEndpoint);
    socketRef.current = socket;

    socket.onopen = () => setConnection('connected');
    socket.onerror = () => setConnection('error');
    socket.onclose = () => {
      if (socketRef.current === socket) {
        socketRef.current = null;
      }
      setConnection('disconnected');
    };
    socket.onmessage = (event) => {
      try {
        handlePacket(JSON.parse(event.data));
      } catch {
        appendToActiveAssistant('Received an invalid assistant event.');
        finishActiveAssistant('error');
      }
    };

    return () => {
      socket.close();
    };
  }, [appendToActiveAssistant, finishActiveAssistant, handlePacket, wsEndpoint]);

  useEffect(() => {
    if (scrollRef.current && 'scrollTo' in scrollRef.current) {
      scrollRef.current.scrollTo({ top: scrollRef.current.scrollHeight });
    }
  }, [messages]);

  const connectionBadge = useMemo(() => {
    if (connection === 'connected') {
      return <Badge color="green" icon="check-circle" text="Streaming connected" />;
    }
    if (connection === 'connecting') {
      return <Badge color="blue" icon="sync" text="Connecting" />;
    }
    if (connection === 'error') {
      return <Badge color="orange" icon="exclamation-triangle" text="REST fallback" />;
    }
    return <Badge color="darkgrey" icon="plug" text="REST fallback" />;
  }, [connection]);

  const submitMessage = async (event?: FormEvent) => {
    event?.preventDefault();
    const content = input.trim();
    if (!content || isStreaming) {
      return;
    }

    const assistantId = createId();
    activeAssistantIdRef.current = assistantId;
    setMessages((current) => [
      ...current,
      { id: createId(), role: 'user', content, status: 'done', toolCalls: [] },
      {
        id: assistantId,
        role: 'assistant',
        content: '',
        status: 'pending',
        thinking: ['Request received.', 'Reading thread context and preparing the model request.'],
        toolCalls: [],
      },
    ]);
    setInput('');
    setIsStreaming(true);

    const socket = socketRef.current;
    if (socket && typeof WebSocket !== 'undefined' && socket.readyState === WebSocket.OPEN) {
      socket.send(JSON.stringify({ type: 'message', thread_id: threadId, content }));
      return;
    }

    try {
      const response = await sendAssistantMessage(threadId, content);
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                content: response.content,
                status: 'done',
                thinking: response.thinking?.length ? response.thinking : message.thinking,
                toolCalls: response.tool_calls.map(normalizeToolCall),
              }
            : message
        )
      );
    } catch (error) {
      setMessages((current) =>
        current.map((message) =>
          message.id === assistantId
            ? {
                ...message,
                content: error instanceof Error ? error.message : 'The assistant request failed.',
                status: 'error',
              }
            : message
        )
      );
    } finally {
      activeAssistantIdRef.current = null;
      setIsStreaming(false);
    }
  };

  const onInputKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      submitMessage();
    }
  };

  const startNewThread = () => {
    const nextThreadId = createId();
    window.localStorage.setItem(threadStorageKey, nextThreadId);
    setThreadId(nextThreadId);
    activeAssistantIdRef.current = null;
    setIsStreaming(false);
    setMessages([
      {
        id: createId(),
        role: 'assistant',
        content: 'Hello! How can I assist you today with your observability data or Grafana dashboards?',
        status: 'done',
        thinking: [],
        toolCalls: [],
      },
    ]);
  };

  return (
    <PluginPage layout={PageLayoutType.Canvas}>
      <div className={s.page} data-testid={testIds.assistant.container}>
        <header className={s.header}>
          <div className={s.heading}>
            <h1 className={s.title}>Assistant</h1>
            <div className={s.metaLine}>
              {connectionBadge}
              {apiKeyConfigured && <Badge color="darkgrey" icon="lock" text="API key" />}
              <span className={s.threadLabel}>Thread {threadId.slice(0, 8)}</span>
            </div>
          </div>
          <IconButton
            name="plus"
            tooltip="New thread"
            aria-label="New thread"
            onClick={startNewThread}
            disabled={isStreaming}
          />
        </header>

        {healthError && (
          <Alert title="Assistant API unavailable" severity="warning">
            {healthError}
          </Alert>
        )}

        <main className={s.chatSurface}>
          <section ref={scrollRef} className={s.messages} aria-live="polite">
            <div className={s.messageList}>
              {messages.map((message) => {
                const isUser = message.role === 'user';
                const isPending = message.status === 'pending' || (message.status === 'streaming' && !message.content);

                return (
                  <article key={message.id} className={isUser ? s.userRow : s.assistantRow}>
                    <div className={isUser ? s.userAvatar : s.assistantAvatar}>{isUser ? 'Y' : 'A'}</div>
                    <div className={s.messageStack}>
                      <div className={isUser ? s.userMeta : s.assistantMeta}>
                        <span>{isUser ? 'You' : 'Assistant'}</span>
                        {message.status === 'streaming' && <Badge color="blue" icon="sync" text="Streaming" />}
                        {message.status === 'error' && <Badge color="red" icon="exclamation-triangle" text="Error" />}
                      </div>
                      <div className={isUser ? s.userBubble : s.assistantBubble}>
                        {!isUser && message.thinking?.length ? (
                          <ThinkingTrail
                            active={message.status === 'pending' || message.status === 'streaming'}
                            styles={s}
                            steps={message.thinking}
                          />
                        ) : null}
                        {message.toolCalls?.length ? (
                          <ToolCallHistory toolCalls={message.toolCalls} styles={s} />
                        ) : null}
                        {isPending ? (
                          <ThinkingIndicator styles={s} />
                        ) : message.content ? (
                          <MarkdownContent content={message.content} renderer={markdown} styles={s} />
                        ) : null}
                      </div>
                    </div>
                  </article>
                );
              })}
            </div>
          </section>

          <form className={s.composer} onSubmit={submitMessage}>
            <TextArea
              data-testid={testIds.assistant.input}
              value={input}
              onChange={(event) => setInput(event.currentTarget.value)}
              onKeyDown={onInputKeyDown}
              rows={1}
              placeholder="Ask the assistant"
              disabled={isStreaming}
            />
            <Button
              data-testid={testIds.assistant.submit}
              type="submit"
              icon="message"
              disabled={!input.trim() || isStreaming}
            >
              Send
            </Button>
          </form>
        </main>
      </div>
    </PluginPage>
  );
}

function MarkdownContent({
  content,
  renderer,
  styles,
}: {
  content: string;
  renderer: MarkdownIt;
  styles: ReturnType<typeof getStyles>;
}) {
  return (
    <div
      className={styles.markdown}
      dangerouslySetInnerHTML={{ __html: renderer.render(content) }}
    />
  );
}

function ThinkingIndicator({ styles }: { styles: ReturnType<typeof getStyles> }) {
  return (
    <div className={styles.thinking}>
      <span>Working</span>
      <span className={styles.dots} aria-hidden="true">
        <span />
        <span />
        <span />
      </span>
    </div>
  );
}

function ThinkingTrail({
  active,
  steps,
  styles,
}: {
  active: boolean;
  steps: string[];
  styles: ReturnType<typeof getStyles>;
}) {
  return (
    <div className={styles.thinkingTrail}>
      <div className={styles.thinkingTrailHeader}>
        <Badge color={active ? 'blue' : 'darkgrey'} icon={active ? 'sync' : 'check'} text="Model thinking" />
        {active && <ThinkingIndicator styles={styles} />}
      </div>
      <ol>
        {steps.map((step, index) => (
          <li key={`${step}-${index}`}>{step}</li>
        ))}
      </ol>
    </div>
  );
}

function ToolCallHistory({
  styles,
  toolCalls,
}: {
  styles: ReturnType<typeof getStyles>;
  toolCalls: AssistantToolCall[];
}) {
  return (
    <div className={styles.toolTimeline}>
      <div className={styles.toolTimelineHeader}>
        <Badge color="blue" icon="cog" text={`${toolCalls.length} tool call${toolCalls.length === 1 ? '' : 's'}`} />
      </div>
      {toolCalls.map((toolCall, index) => {
        const parameters = formatValue(toolCall.parameters);
        const result = formatValue(toolCall.result);

        return (
          <details key={`${toolCall.tool}-${index}`} className={styles.toolCall}>
            <summary>
              <span className={styles.toolName}>{toolCall.tool}</span>
              {result && <span className={styles.toolPreview}>{compactText(result)}</span>}
            </summary>
            {parameters && (
              <div className={styles.toolSection}>
                <span>Parameters</span>
                <pre>{parameters}</pre>
              </div>
            )}
            {result && (
              <div className={styles.toolSection}>
                <span>Result</span>
                <pre>{result}</pre>
              </div>
            )}
          </details>
        );
      })}
    </div>
  );
}

export default AssistantPage;

const getStyles = (theme: GrafanaTheme2) => ({
  page: css`
    display: grid;
    gap: ${theme.spacing(2)};
    min-height: calc(100vh - 72px);
    padding: ${theme.spacing(2)} ${theme.spacing(3)};
  `,
  header: css`
    align-items: center;
    border-bottom: 1px solid ${theme.colors.border.weak};
    display: flex;
    gap: ${theme.spacing(2)};
    justify-content: space-between;
    padding-bottom: ${theme.spacing(1.5)};
  `,
  heading: css`
    min-width: 0;
  `,
  title: css`
    font-size: ${theme.typography.h4.fontSize};
    font-weight: ${theme.typography.fontWeightMedium};
    line-height: ${theme.typography.h4.lineHeight};
    margin: 0 0 ${theme.spacing(0.75)};
  `,
  metaLine: css`
    align-items: center;
    display: flex;
    flex-wrap: wrap;
    gap: ${theme.spacing(1)};
  `,
  threadLabel: css`
    color: ${theme.colors.text.secondary};
    font-size: ${theme.typography.bodySmall.fontSize};
  `,
  chatSurface: css`
    background: ${theme.colors.background.primary};
    display: grid;
    grid-template-rows: minmax(0, 1fr) auto;
    height: calc(100vh - 190px);
    min-height: 460px;
    overflow: hidden;
  `,
  messages: css`
    overflow-y: auto;
    padding: ${theme.spacing(3)} ${theme.spacing(2)};
  `,
  messageList: css`
    display: flex;
    flex-direction: column;
    gap: ${theme.spacing(2.25)};
    margin: 0 auto;
    max-width: 1040px;
    width: 100%;
  `,
  assistantRow: css`
    align-items: flex-start;
    display: flex;
    gap: ${theme.spacing(1.25)};
    max-width: 920px;
  `,
  userRow: css`
    align-items: flex-start;
    align-self: flex-end;
    display: flex;
    flex-direction: row-reverse;
    gap: ${theme.spacing(1.25)};
    max-width: min(760px, 88%);
  `,
  assistantAvatar: css`
    align-items: center;
    background: ${theme.colors.background.secondary};
    border: 1px solid ${theme.colors.border.weak};
    border-radius: 50%;
    color: ${theme.colors.text.secondary};
    display: flex;
    flex: 0 0 28px;
    font-size: ${theme.typography.bodySmall.fontSize};
    font-weight: ${theme.typography.fontWeightMedium};
    height: 28px;
    justify-content: center;
    margin-top: ${theme.spacing(2.75)};
    width: 28px;
  `,
  userAvatar: css`
    align-items: center;
    background: ${theme.colors.primary.transparent};
    border: 1px solid ${theme.colors.primary.border};
    border-radius: 50%;
    color: ${theme.colors.primary.text};
    display: flex;
    flex: 0 0 28px;
    font-size: ${theme.typography.bodySmall.fontSize};
    font-weight: ${theme.typography.fontWeightMedium};
    height: 28px;
    justify-content: center;
    margin-top: ${theme.spacing(2.75)};
    width: 28px;
  `,
  messageStack: css`
    display: grid;
    gap: ${theme.spacing(0.75)};
    min-width: 0;
    width: 100%;
  `,
  assistantMeta: css`
    align-items: center;
    color: ${theme.colors.text.secondary};
    display: flex;
    font-size: ${theme.typography.bodySmall.fontSize};
    gap: ${theme.spacing(1)};
  `,
  userMeta: css`
    align-items: center;
    color: ${theme.colors.text.secondary};
    display: flex;
    flex-direction: row-reverse;
    font-size: ${theme.typography.bodySmall.fontSize};
    gap: ${theme.spacing(1)};
  `,
  assistantBubble: css`
    background: ${theme.colors.background.secondary};
    border: 1px solid ${theme.colors.border.weak};
    border-radius: ${theme.shape.radius.default};
    color: ${theme.colors.text.primary};
    display: grid;
    gap: ${theme.spacing(1.5)};
    line-height: 1.55;
    min-width: 0;
    overflow: hidden;
    padding: ${theme.spacing(1.75)} ${theme.spacing(2)};
  `,
  userBubble: css`
    background: ${theme.colors.primary.transparent};
    border: 1px solid ${theme.colors.primary.border};
    border-radius: ${theme.shape.radius.default};
    color: ${theme.colors.text.primary};
    line-height: 1.5;
    overflow-wrap: anywhere;
    padding: ${theme.spacing(1.25)} ${theme.spacing(1.5)};
  `,
  markdown: css`
    max-width: 100%;
    min-width: 0;
    overflow-x: auto;
    overflow-wrap: anywhere;
    padding-bottom: ${theme.spacing(0.5)};

    scrollbar-color: ${theme.colors.border.medium} transparent;
    scrollbar-width: thin;

    > :first-child {
      margin-top: 0;
    }

    > :last-child {
      margin-bottom: 0;
    }

    p {
      margin: 0 0 ${theme.spacing(1.25)};
    }

    h1,
    h2,
    h3,
    h4 {
      color: ${theme.colors.text.primary};
      font-weight: ${theme.typography.fontWeightMedium};
      line-height: 1.25;
      margin: ${theme.spacing(2)} 0 ${theme.spacing(1)};
    }

    h1 {
      font-size: ${theme.typography.h3.fontSize};
    }

    h2 {
      font-size: ${theme.typography.h4.fontSize};
    }

    h3,
    h4 {
      font-size: ${theme.typography.h5.fontSize};
    }

    ul,
    ol {
      margin: 0 0 ${theme.spacing(1.25)} ${theme.spacing(2.5)};
      padding: 0;
    }

    li {
      margin: ${theme.spacing(0.5)} 0;
    }

    a {
      color: ${theme.colors.text.link};
    }

    blockquote {
      border-left: 3px solid ${theme.colors.border.medium};
      color: ${theme.colors.text.secondary};
      margin: ${theme.spacing(1.5)} 0;
      padding-left: ${theme.spacing(1.5)};
    }

    code {
      background: ${theme.colors.background.primary};
      border: 1px solid ${theme.colors.border.weak};
      border-radius: ${theme.shape.radius.default};
      font-size: 0.92em;
      padding: 0 ${theme.spacing(0.5)};
    }

    pre {
      background: ${theme.colors.background.primary};
      border: 1px solid ${theme.colors.border.weak};
      border-radius: ${theme.shape.radius.default};
      margin: ${theme.spacing(1.25)} 0;
      max-width: 100%;
      overflow: auto;
      padding: ${theme.spacing(1.25)};

      code {
        background: transparent;
        border: 0;
        padding: 0;
      }
    }

    table {
      border-collapse: collapse;
      display: table;
      margin: ${theme.spacing(1.25)} 0;
      max-width: none;
      min-width: 100%;
      width: max-content;
    }

    th,
    td {
      border: 1px solid ${theme.colors.border.weak};
      padding: ${theme.spacing(0.75)} ${theme.spacing(1)};
      text-align: left;
      vertical-align: top;
      white-space: nowrap;
    }

    th {
      background: ${theme.colors.background.primary};
      font-weight: ${theme.typography.fontWeightMedium};
    }
  `,
  thinking: css`
    align-items: center;
    color: ${theme.colors.text.secondary};
    display: inline-flex;
    gap: ${theme.spacing(1)};
  `,
  thinkingTrail: css`
    background: ${theme.colors.background.primary};
    border: 1px solid ${theme.colors.border.weak};
    border-radius: ${theme.shape.radius.default};
    display: grid;
    gap: ${theme.spacing(1)};
    padding: ${theme.spacing(1)};

    ol {
      color: ${theme.colors.text.secondary};
      display: grid;
      font-size: ${theme.typography.bodySmall.fontSize};
      gap: ${theme.spacing(0.5)};
      margin: 0 0 0 ${theme.spacing(2)};
      padding: 0;
    }

    li::marker {
      color: ${theme.colors.text.disabled};
    }
  `,
  thinkingTrailHeader: css`
    align-items: center;
    display: flex;
    flex-wrap: wrap;
    gap: ${theme.spacing(1)};
  `,
  dots: css`
    display: inline-flex;
    gap: ${theme.spacing(0.5)};

    span {
      animation: assistantPulse 1.2s infinite ease-in-out;
      background: ${theme.colors.text.secondary};
      border-radius: 50%;
      display: block;
      height: 5px;
      opacity: 0.45;
      width: 5px;
    }

    span:nth-child(2) {
      animation-delay: 0.16s;
    }

    span:nth-child(3) {
      animation-delay: 0.32s;
    }

    @keyframes assistantPulse {
      0%,
      80%,
      100% {
        transform: scale(0.72);
      }
      40% {
        opacity: 1;
        transform: scale(1);
      }
    }
  `,
  toolTimeline: css`
    border-bottom: 1px solid ${theme.colors.border.weak};
    display: grid;
    gap: ${theme.spacing(1)};
    padding-bottom: ${theme.spacing(1.25)};
  `,
  toolTimelineHeader: css`
    align-items: center;
    display: flex;
    justify-content: flex-start;
  `,
  toolCall: css`
    background: ${theme.colors.background.primary};
    border: 1px solid ${theme.colors.border.weak};
    border-radius: ${theme.shape.radius.default};
    overflow: hidden;

    summary {
      align-items: center;
      cursor: pointer;
      display: grid;
      gap: ${theme.spacing(1)};
      grid-template-columns: minmax(180px, max-content) minmax(0, 1fr);
      list-style: none;
      padding: ${theme.spacing(1)};
    }

    summary::-webkit-details-marker {
      display: none;
    }

    &[open] summary {
      border-bottom: 1px solid ${theme.colors.border.weak};
    }

    @media (max-width: 720px) {
      summary {
        grid-template-columns: 1fr;
      }
    }
  `,
  toolName: css`
    color: ${theme.colors.text.primary};
    font-family: ${theme.typography.fontFamilyMonospace};
    font-size: ${theme.typography.bodySmall.fontSize};
    font-weight: ${theme.typography.fontWeightMedium};
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  `,
  toolPreview: css`
    color: ${theme.colors.text.secondary};
    font-size: ${theme.typography.bodySmall.fontSize};
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  `,
  toolSection: css`
    display: grid;
    gap: ${theme.spacing(0.75)};
    padding: ${theme.spacing(1)};

    span {
      color: ${theme.colors.text.secondary};
      font-size: ${theme.typography.bodySmall.fontSize};
      font-weight: ${theme.typography.fontWeightMedium};
    }

    pre {
      background: ${theme.colors.background.secondary};
      border: 1px solid ${theme.colors.border.weak};
      border-radius: ${theme.shape.radius.default};
      color: ${theme.colors.text.primary};
      margin: 0;
      max-height: 280px;
      overflow: auto;
      padding: ${theme.spacing(1)};
      white-space: pre-wrap;
    }
  `,
  composer: css`
    align-items: flex-end;
    border-top: 1px solid ${theme.colors.border.weak};
    display: grid;
    gap: ${theme.spacing(1)};
    grid-template-columns: minmax(0, 1fr) auto;
    margin: 0 auto;
    max-width: 1040px;
    padding: ${theme.spacing(1.5)} ${theme.spacing(2)} ${theme.spacing(2)};
    width: 100%;

    textarea {
      max-height: 180px;
      min-height: 44px;
      resize: vertical;
    }

    button {
      min-height: 40px;
    }

    @media (max-width: 700px) {
      grid-template-columns: 1fr;
    }
  `,
});
