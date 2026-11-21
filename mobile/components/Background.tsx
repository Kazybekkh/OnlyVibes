// App backdrop (light): soft system-grouped gradient with faint pastel glows
// so the glass cards/tab bar have something to blur over (Liquid Glass reads
// as frosted white instead of flat gray).
import { LinearGradient } from 'expo-linear-gradient';
import { StyleSheet, View } from 'react-native';

export function Background() {
  return (
    <View style={StyleSheet.absoluteFill} pointerEvents="none">
      <LinearGradient
        colors={['#f2f2f7', '#ffffff', '#eef1f7']}
        locations={[0, 0.55, 1]}
        style={StyleSheet.absoluteFill}
      />
      <View style={[styles.glow, { backgroundColor: '#007aff', top: -140, right: -120, opacity: 0.10 }]} />
      <View style={[styles.glow, { backgroundColor: '#5ac8fa', bottom: -180, left: -140, opacity: 0.14 }]} />
      <View style={[styles.glow, { backgroundColor: '#ff375f', top: '38%', left: '55%', opacity: 0.06, width: 260, height: 260 }]} />
    </View>
  );
}

const styles = StyleSheet.create({
  glow: {
    position: 'absolute',
    width: 420,
    height: 420,
    borderRadius: 999,
  },
});
