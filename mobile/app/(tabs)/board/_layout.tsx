import { Stack } from 'expo-router';
import { headerOptions } from '../../../lib/headerOptions';

export default function BoardStack() {
  return (
    <Stack screenOptions={headerOptions}>
      <Stack.Screen name="index" options={{ title: 'Board', headerTitle: 'Board' }} />
    </Stack>
  );
}
