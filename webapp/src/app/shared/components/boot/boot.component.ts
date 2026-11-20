import { Component, Input } from '@angular/core';

@Component({
  selector: 'app-boot',
  standalone: false,
  templateUrl: './boot.component.html',
  styleUrls: ['./boot.component.scss'],
})
export class BootComponent {
  @Input() message: string = '';
}