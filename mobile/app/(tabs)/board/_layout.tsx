import { Stack } from 'expo-router';
import { headerOptions } from '../../../lib/headerOptions';
import { wordmarkHeaderTitle } from '../../../lib/Wordmark';

export default function BoardStack() {
  return (
    // large title off: the native large-title view renders the text 'title',
    // which would hide the wordmark until scroll — inline center view shows it
    <Stack screenOptions={{ ...headerOptions, headerLargeTitle: false }}>
      <Stack.Screen name="index" options={{ title: 'Board', headerTitle: wordmarkHeaderTitle }} />
    </Stack>
  );
}
