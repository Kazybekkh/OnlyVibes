import { Stack } from 'expo-router';
import { headerOptions } from '../../../lib/headerOptions';
import { wordmarkHeaderTitle } from '../../../lib/Wordmark';

export default function AlertsStack() {
  return (
    <Stack screenOptions={headerOptions}>
      <Stack.Screen name="index" options={{ title: 'Alerts', headerTitle: wordmarkHeaderTitle }} />
    </Stack>
  );
}
