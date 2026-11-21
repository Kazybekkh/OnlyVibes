import { Stack } from 'expo-router';
import { headerOptions } from '../../../lib/headerOptions';

export default function AlertsStack() {
  return (
    <Stack screenOptions={headerOptions}>
      <Stack.Screen name="index" options={{ title: 'Alerts', headerTitle: 'Alerts' }} />
    </Stack>
  );
}
