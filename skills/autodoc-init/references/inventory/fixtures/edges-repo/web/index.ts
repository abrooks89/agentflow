import { fmt } from './util';

export function render(): string {
  return fmt('x');
}

export class View {
  draw(): string {
    return render();
  }
}
