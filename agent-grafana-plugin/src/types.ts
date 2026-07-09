export type AppPluginSettings = {
  assistantApiUrl?: string;
  assistantWsUrl?: string;
  apiUrl?: string;
};

export type AssistantRole = 'assistant' | 'user' | 'system';

export type AssistantToolCall = {
  tool: string;
  parameters?: Record<string, unknown>;
  result?: string;
};

export type AssistantMessage = {
  id: string;
  role: AssistantRole;
  content: string;
  status?: 'pending' | 'streaming' | 'done' | 'error';
  thinking?: string[];
  toolCalls?: AssistantToolCall[];
};

export type AssistantSettings = {
  assistantApiUrl: string;
  assistantWsUrl: string;
  apiKeyConfigured: boolean;
};

export type AssistantPacket =
  | { type: 'ready'; thread_id: string }
  | { type: 'ack'; thread_id: string }
  | ({ type: 'tool_call' } & AssistantToolCall)
  | { type: 'thinking'; content: string }
  | { type: 'token'; content: string }
  | { type: 'message'; role?: AssistantRole; content: string }
  | {
      type: 'done';
      thread_id: string;
      conversation_history_length: number;
      model: string;
      provider: string;
    }
  | { type: 'error'; content: string };
