import React from 'react';
import { Route, Routes } from 'react-router-dom';
import { AppRootProps } from '@grafana/data';
import { ROUTES } from '../../constants';
const AssistantPage = React.lazy(() => import('../../pages/AssistantPage'));

function App(props: AppRootProps) {
  return (
    <Routes>
      <Route path={ROUTES.Assistant} element={<AssistantPage meta={props.meta} />} />
      <Route path="*" element={<AssistantPage meta={props.meta} />} />
    </Routes>
  );
}

export default App;
