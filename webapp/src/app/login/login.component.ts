import { Component, EventEmitter, Input, Output } from '@angular/core';

@Component({
  selector: 'app-login',
  standalone: false,
  templateUrl: './login.component.html',
  styleUrls: ['./login.component.scss'],
})
export class LoginComponent {
  @Input() loading: boolean = false;
  @Input() error: string = '';

  @Output() readonly signIn = new EventEmitter<void>();
  @Output() readonly themeToggle = new EventEmitter<void>();
}