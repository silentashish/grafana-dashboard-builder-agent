import { lastValueFrom } from 'rxjs';
import { getBackendSrv } from '@grafana/runtime';
import pluginJson from '../plugin.json';
import { AssistantSettings, AssistantToolCall } from '../types';

export type ChatResponse = {
  thread_id: string;
  content: string;
  tool_calls: AssistantToolCall[];
  thinking?: string[];
  conversation_history_length: number;
  model: string;
  provider: string;
};

const resourceUrl = (path: string) => `/api/plugins/${pluginJson.id}/resources/${path}`;

export async function loadAssistantSettings(): Promise<AssistantSettings> {
  const response = await lastValueFrom(
    getBackendSrv().fetch<AssistantSettings>({
      url: resourceUrl('assistant/settings'),
      method: 'GET',
    })
  );
  return response.data;
}

export async function loadAssistantHealth(): Promise<{ status: string }> {
  const response = await lastValueFrom(
    getBackendSrv().fetch<{ status: string }>({
      url: resourceUrl('assistant/health'),
      method: 'GET',
    })
  );
  return response.data;
}

export async function sendAssistantMessage(threadId: string, message: string): Promise<ChatResponse> {
  const response = await lastValueFrom(
    getBackendSrv().fetch<ChatResponse>({
      url: resourceUrl('assistant/chat'),
      method: 'POST',
      data: {
        thread_id: threadId,
        message,
      },
    })
  );
  return response.data;
}

export function deriveWebSocketUrl(apiUrl: string, threadId: string): string {
  if (!apiUrl) {
    return '';
  }

  const url = new URL(apiUrl, window.location.origin);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = `${url.pathname.replace(/\/$/, '')}/ws/assistant`;
  url.searchParams.set('thread_id', threadId);
  return url.toString();
}

export function withThreadId(wsUrl: string, threadId: string): string {
  if (!wsUrl) {
    return '';
  }

  const url = new URL(wsUrl, window.location.origin);
  url.searchParams.set('thread_id', threadId);
  return url.toString();
}
