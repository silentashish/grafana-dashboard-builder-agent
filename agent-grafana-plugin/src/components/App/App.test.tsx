import React from 'react';
import { MemoryRouter } from 'react-router-dom';
import { AppRootProps, PluginType } from '@grafana/data';
import { render, waitFor } from '@testing-library/react';
import { of } from 'rxjs';
import App from './App';

jest.mock('@grafana/runtime', () => ({
  PluginPage: ({ children }: { children: React.ReactNode }) => children,
  getBackendSrv: () => ({
    fetch: jest.fn(({ url }) =>
      of({
        data: url.includes('settings')
          ? {
              assistantApiUrl: 'http://assistant-api:8000',
              assistantWsUrl: '',
              apiKeyConfigured: false,
            }
          : { status: 'ok' },
      })
    ),
  }),
}));

describe('Components/App', () => {
  let props: AppRootProps;

  beforeEach(() => {
    jest.resetAllMocks();

    props = {
      basename: 'a/sample-app',
      meta: {
        id: 'sample-app',
        name: 'Sample App',
        type: PluginType.app,
        enabled: true,
        jsonData: {
          assistantApiUrl: 'http://assistant-api:8000',
          assistantWsUrl: '',
        },
      },
      query: {},
      path: '',
      onNavChanged: jest.fn(),
    } as unknown as AppRootProps;
  });

  test('renders without an error"', async () => {
    const { queryByText } = render(
      <MemoryRouter>
        <App {...props} />
      </MemoryRouter>
    );

    // Application is lazy loaded, so we need to wait for the component and routes to be rendered
    await waitFor(() => expect(queryByText(/how can i assist you today/i)).toBeInTheDocument(), { timeout: 2000 });
  });
});
